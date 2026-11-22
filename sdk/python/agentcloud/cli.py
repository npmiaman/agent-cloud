"""`ac`: Agent Cloud from any runtime that can run a command.

    export AGENTCLOUD_TOKEN=aca_...            # the agent's token
    export AGENTCLOUD_URL=http://127.0.0.1:8787
    ac ls -r
    ac put report.md reports/q3.md -m "first draft"
    ac cat reports/q3.md
    ac log reports/q3.md
    ac restore reports/q3.md 1

Workspace: --workspace / $AGENTCLOUD_WORKSPACE, or the only workspace the token can reach.
Exit codes: 0 ok, 1 error, 3 change is waiting for human approval.
Add --json for machine-readable output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import AgentCloud, AgentCloudError, Pending, Version, Workspace

PENDING_EXIT = 3


def _ts(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def _size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def emit(self, data, text: str | None = None):
        if self.as_json:
            print(json.dumps(data, default=lambda o: o.__dict__, indent=1))
        elif text is not None:
            print(text)

    def result(self, r) -> int:
        if isinstance(r, Pending):
            self.emit(r, f"pending approval: {r.path} (proposal {r.proposal_id}); the original is unchanged")
            return PENDING_EXIT
        if isinstance(r, Version):
            self.emit(r, f"{r.path} v{r.version} ({r.op})")
        else:
            self.emit(r, json.dumps(r))
        return 0


def pick_workspace(ac: AgentCloud, explicit: str | None) -> Workspace:
    ws_id = explicit or os.environ.get("AGENTCLOUD_WORKSPACE")
    if ws_id:
        return ac.workspace(ws_id)
    found = ac.workspaces()
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit("no workspace is reachable with this token")
    names = "\n".join(f"  {w.id}  {w.info.get('name')}" for w in found)
    raise SystemExit(f"this token reaches {len(found)} workspaces; pick one with --workspace or AGENTCLOUD_WORKSPACE:\n{names}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ac", description="Agent Cloud CLI. Token from $AGENTCLOUD_TOKEN.")
    ap.add_argument("--url", help="server URL (default $AGENTCLOUD_URL or http://127.0.0.1:8787)")
    ap.add_argument("--workspace", "-w", help="workspace id (default $AGENTCLOUD_WORKSPACE or the only one)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="show this token's identity and access")
    sub.add_parser("workspaces", help="list reachable workspaces")
    p = sub.add_parser("ls", help="list files"); p.add_argument("prefix", nargs="?", default="")
    p.add_argument("-r", "--recursive", action="store_true"); p.add_argument("-q", "--query"); p.add_argument("--deleted", action="store_true")
    p = sub.add_parser("cat", help="print a file"); p.add_argument("path"); p.add_argument("--version", type=int)
    p = sub.add_parser("get", help="download a file"); p.add_argument("path"); p.add_argument("dest", nargs="?"); p.add_argument("--version", type=int)
    p = sub.add_parser("put", help="upload a file ('-' reads stdin)"); p.add_argument("src"); p.add_argument("dest", nargs="?")
    p.add_argument("-m", "--message", default=""); p.add_argument("--if-version", type=int, help="fail unless the file is at this version (0 = must not exist)")
    p.add_argument("--type", dest="content_type")
    p = sub.add_parser("rm", help="delete (restorable)"); p.add_argument("path"); p.add_argument("-m", "--message", default="")
    p = sub.add_parser("mv", help="move / rename"); p.add_argument("src"); p.add_argument("dst")
    p = sub.add_parser("mkdir", help="create a folder"); p.add_argument("path")
    p = sub.add_parser("stat", help="file metadata"); p.add_argument("path")
    p = sub.add_parser("log", help="version history"); p.add_argument("path")
    p = sub.add_parser("diff", help="compare versions"); p.add_argument("path"); p.add_argument("a", type=int); p.add_argument("b", type=int, nargs="?")
    p = sub.add_parser("restore", help="make an old version current"); p.add_argument("path"); p.add_argument("version", type=int)
    p = sub.add_parser("push", help="upload a local directory"); p.add_argument("dir"); p.add_argument("prefix", nargs="?", default="")
    p.add_argument("-m", "--message", default="")
    p = sub.add_parser("pull", help="download into a local directory"); p.add_argument("dir"); p.add_argument("prefix", nargs="?", default="")
    p = sub.add_parser("share", help="give another agent access"); p.add_argument("agent_id"); p.add_argument("path", nargs="?", default="")
    p.add_argument("--write", action="store_true"); p.add_argument("--hours", type=float)
    p = sub.add_parser("status", help="check a pending proposal"); p.add_argument("proposal_id")
    return ap


def main(argv: list[str] | None = None, client: AgentCloud | None = None) -> int:
    a = build_parser().parse_args(argv)
    out = Out(a.json)
    ac = client or AgentCloud(base_url=a.url)
    if not ac.token and client is None:
        print("set AGENTCLOUD_TOKEN to an agent token (or a user API key)", file=sys.stderr)
        return 1
    try:
        if a.cmd == "whoami":
            me = ac.me()
            lines = [f"{me['kind']} {me.get('name') or me.get('email')} ({me['id']})"]
            for g in me.get("grants", []):
                rights = ",".join(k for k in ("read", "write", "delete", "share") if g[k])
                lines.append(f"  {g['workspace_name']} ({g['workspace_id']}) /{g['path']}  {rights}  approval={g['approval']}")
            out.emit(me, "\n".join(lines)); return 0
        if a.cmd == "workspaces":
            ws = ac.workspaces()
            out.emit([w.info for w in ws], "\n".join(f"{w.id}  {w.info['name']}  {_size(w.info['used_bytes'])} / {_size(w.info['quota_bytes'])}" for w in ws)); return 0
        if a.cmd == "status":
            p = ac.proposal(a.proposal_id)
            out.emit(p, p["status"]); return 0

        ws = pick_workspace(ac, a.workspace)
        if a.cmd == "ls":
            es = ws.list(a.prefix, recursive=a.recursive or bool(a.query), query=a.query, include_deleted=a.deleted)
            out.emit(es, "\n".join(
                f"{e['path']}" if e["kind"] == "folder" else
                f"{e['path']:<40} v{e['version']:<3} {_size(e['size']):>9}  {_ts(e['updated_at'])}  {e['updated_by'] or ''}{'  (deleted)' if e['deleted'] else ''}"
                for e in es)); return 0
        if a.cmd == "cat":
            data = ws.read(a.path, a.version)
            sys.stdout.buffer.write(data); sys.stdout.flush(); return 0
        if a.cmd == "get":
            dest = ws.download(a.path, a.dest or Path(a.path).name, a.version)
            out.emit({"path": a.path, "saved_to": str(dest)}, f"saved {dest}"); return 0
        if a.cmd == "put":
            data = sys.stdin.buffer.read() if a.src == "-" else Path(a.src).read_bytes()
            dest = a.dest or (Path(a.src).name if a.src != "-" else None)
            if not dest:
                raise SystemExit("put from stdin needs a destination path")
            return out.result(ws.write(dest, data, content_type=a.content_type, if_version=a.if_version, message=a.message))
        if a.cmd == "rm":
            return out.result(ws.delete(a.path, message=a.message))
        if a.cmd == "mv":
            r = ws.move(a.src, a.dst)
            out.emit(r, "\n".join(f"{m['from']} -> {m['to']} (v{m['version']})" for m in r["moved"])); return 0
        if a.cmd == "mkdir":
            out.emit(ws.mkdir(a.path), f"{a.path.rstrip('/')}/"); return 0
        if a.cmd == "stat":
            s = ws.stat(a.path)
            out.emit(s, "\n".join(f"{k}: {v}" for k, v in s.items())); return 0
        if a.cmd == "log":
            vs = ws.versions(a.path)
            out.emit(vs, "\n".join(f"v{v['n']:<3} {v['op']:<8} {_size(v['size']) if v['blob_sha256'] else '-':>9}  {_ts(v['created_at'])}  {v['actor']}  {v['message']}" for v in vs)); return 0
        if a.cmd == "diff":
            d = ws.diff(a.path, a.a, a.b)
            text = d.get("unified") or ("identical" if d["identical"] else f"binary: {d['size_from']} -> {d['size_to']} bytes")
            if d.get("csv"):
                c = d["csv"]
                text = f"columns removed: {c['columns_removed']}  added: {c['columns_added']}  rows: {c['rows_before']} -> {c['rows_after']}\n" + text
            out.emit(d, text); return 0
        if a.cmd == "restore":
            return out.result(ws.restore(a.path, a.version))
        if a.cmd == "push":
            rs = ws.push_dir(a.dir, a.prefix, a.message)
            pending = [r for r in rs if isinstance(r, Pending)]
            out.emit(rs, "\n".join(f"{r.path}: pending approval ({r.proposal_id})" if isinstance(r, Pending) else f"{r.path}: v{r.version} ({r.op})" for r in rs))
            return PENDING_EXIT if pending else 0
        if a.cmd == "pull":
            ps = ws.pull_dir(a.dir, a.prefix)
            out.emit([str(p) for p in ps], f"{len(ps)} file(s) -> {a.dir}"); return 0
        if a.cmd == "share":
            g = ws.share(a.agent_id, a.path, read=True, write=a.write, ttl_seconds=int(a.hours * 3600) if a.hours else None)
            out.emit(g, f"shared /{g['path']} with {a.agent_id} ({'read+write' if g['write'] else 'read'}, grant {g['id']})"); return 0
    except AgentCloudError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


def entry():
    sys.exit(main())


if __name__ == "__main__":
    entry()
