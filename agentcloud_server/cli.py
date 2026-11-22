"""`agentcloud`: run and operate a self-hosted Agent Cloud from the terminal.

    agentcloud up                          start (first run creates a local admin + login link)
    agentcloud agent create research       new agent + workspace + credential, with MCP configs
    agentcloud agent list
    agentcloud approvals                   list pending changes; approve/reject <id>
    agentcloud webhook add https://…       signed event webhooks (e.g. --events approval.*)
    agentcloud login-link                  fresh one-time dashboard login link
    agentcloud status                      health + instance facts
    agentcloud worker [--once]             maintenance worker (expiry, GC, retention, webhooks)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

DEFAULT_PORT = 8787


def _settings(data: str | None):
    from .config import Settings
    s = Settings()
    if data:
        s.data_dir = Path(data).expanduser().resolve()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def _server_file(s) -> Path:
    return s.data_dir / "server.json"


def _url(s) -> str:
    if os.environ.get("AGENTCLOUD_URL"):
        return os.environ["AGENTCLOUD_URL"].rstrip("/")
    f = _server_file(s)
    if f.exists():
        return json.loads(f.read_text())["url"]
    return f"http://127.0.0.1:{DEFAULT_PORT}"


def _admin_client(s):
    import httpx
    from . import local
    key = os.environ.get("AGENTCLOUD_ADMIN_KEY") or (local.load_admin(s.data_dir) or {}).get("api_key")
    if not key:
        sys.exit(f"no admin key: start the server once with `agentcloud up --data {s.data_dir}` or set AGENTCLOUD_ADMIN_KEY")
    return httpx.Client(base_url=_url(s), headers={"Authorization": f"Bearer {key}"}, timeout=30)


def _call(client, method, path, **kw):
    import httpx
    try:
        r = client.request(method, path, **kw)
    except httpx.ConnectError:
        sys.exit(f"can't reach {client.base_url}: is `agentcloud up` running?")
    if r.status_code >= 400:
        err = (r.json().get("error") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
        sys.exit(f"error {r.status_code} {err.get('code', '')}: {err.get('message', r.text[:200])}")
    return r.json()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


# ---------------------------------------------------------------- commands

def cmd_up(a):
    import uvicorn
    from . import local, webhooks, worker
    from .app import create_app
    s = _settings(a.data)
    url = f"http://{a.host if a.host not in ('0.0.0.0', '::') else '127.0.0.1'}:{a.port}"
    app = create_app(s)                       # migrations + bootstrap happen once, here
    db = app.state.ctx.db
    created = local.bootstrap(db, s.data_dir)
    admin = local.load_admin(s.data_dir) or {}
    _server_file(s).write_text(json.dumps({"url": url, "pid": os.getpid()}))
    workers = (min(4, os.cpu_count() or 1) if a.workers == "auto" else int(a.workers))
    link = local.magic_link(db, admin["user_id"], url) if admin.get("user_id") else None
    print()
    print(f"  {_c('1', 'Agent Cloud')} {app.version}  ·  {_c('36', url)}")
    print(f"  data     {s.data_dir}")
    print(f"  workers  {workers} HTTP process{'es' if workers > 1 else ''}" + ("" if a.no_worker else " + maintenance + webhooks"))
    if link:
        print(f"  login    {_c('36', link)}  {_c('2', '(one-time, 15 min)')}")
    if created:
        print(f"  admin    created admin@localhost · credentials in {local.admin_file(s.data_dir)}")
    print(f"  next     {_c('1', 'agentcloud agent create my-agent')}")
    print()
    if not a.no_worker:
        threading.Thread(target=worker.run_forever, args=(app.state.ctx, 60, s.data_dir), daemon=True, name="maintenance").start()
        threading.Thread(target=webhooks.run_forever, args=(db,), daemon=True, name="webhooks").start()
    fast = {}
    for mod, key, val in (("uvloop", "loop", "uvloop"), ("httptools", "http", "httptools")):
        try:
            __import__(mod)
            fast[key] = val
        except ImportError:
            pass
    if workers > 1:
        os.environ["AGENTCLOUD_DATA"] = str(s.data_dir)
        os.environ["AGENTCLOUD_INLINE_WORKER"] = "0"
        uvicorn.run("agentcloud_server.app:create_app", factory=True, host=a.host, port=a.port, workers=workers,
                    access_log=False, log_level="warning", **fast)
    else:
        uvicorn.run(app, host=a.host, port=a.port, access_log=False, log_level="warning", **fast)


def cmd_worker(a):
    from . import webhooks, worker
    from .app import create_app
    s = _settings(a.data)
    app = create_app(s)
    if a.once:
        print(json.dumps({**worker.run_once(app.state.ctx, data_dir=s.data_dir), "webhooks": webhooks.deliver_due(app.state.ctx.db)}))
        return
    threading.Thread(target=webhooks.run_forever, args=(app.state.ctx.db,), daemon=True).start()
    worker.run_forever(app.state.ctx, 60, s.data_dir)


def cmd_login_link(a):
    from . import local
    from .db import Database
    s = _settings(a.data)
    admin = local.load_admin(s.data_dir)
    db = Database(s.data_dir / "agentcloud.db")
    uid = admin["user_id"] if admin else (db.one("SELECT id FROM users ORDER BY created_at LIMIT 1") or {"id": None})["id"]
    if not uid:
        sys.exit("no users yet: run `agentcloud up` first")
    print(local.magic_link(db, uid, _url(s)))


def connect_snippets(url: str, token: str, name: str = "agent-cloud") -> dict:
    mcp = f"{url}/mcp"
    return {
        "claude": f'claude mcp add --transport http {name} {mcp} --header "Authorization: Bearer {token}"',
        "json": json.dumps({"mcpServers": {name: {"type": "http", "url": mcp, "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2),
        "cursor": json.dumps({"mcpServers": {name: {"url": mcp, "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2),
        "env": f"export AGENTCLOUD_URL={url}\nexport AGENTCLOUD_TOKEN={token}",
    }


def cmd_agent_create(a):
    s = _settings(a.data)
    c = _admin_client(s)
    body = {"name": a.name, "access": a.access, "approval": a.approval}
    if a.storage:
        from agentcloud_server.cli import parse_size
        body["storage_mb"] = max(1, parse_size(a.storage) // (1024 * 1024))
    out = _call(c, "POST", "/v1/agents/provision", json=body)
    tok, url = out["token"]["token"], _url(s)
    snip = connect_snippets(url, tok)
    if a.json:
        print(json.dumps({"agent_id": out["agent"]["id"], "workspace_id": out["workspace"]["id"], "credential": tok, "connect": snip}, indent=2))
        return
    print(f"\n  {_c('1', out['agent']['name'])}  {out['agent']['id']}")
    print(f"  workspace  {out['workspace']['id']}  ({out['workspace']['storage_limit_bytes'] // (1024 * 1024)} MB, {a.access}, approval: {a.approval})")
    print(f"  credential {_c('33', tok)}  {_c('2', '(shown once)')}\n")
    if a.client in ("claude", "all"):
        print(_c("2", "  # Claude Code"))
        print("  " + snip["claude"] + "\n")
    if a.client in ("cursor", "all"):
        print(_c("2", "  # Cursor (~/.cursor/mcp.json) and most MCP clients"))
        print("\n".join("  " + line for line in snip["cursor"].splitlines()) + "\n")
    if a.client in ("env", "all"):
        print(_c("2", "  # SDKs and the `ac` CLI"))
        print("\n".join("  " + line for line in snip["env"].splitlines()) + "\n")


def cmd_agent_list(a):
    s = _settings(a.data)
    rows = _call(_admin_client(s), "GET", "/v1/agents")
    if a.json:
        print(json.dumps(rows, indent=2)); return
    for r in rows:
        creds = sum(1 for t in r["tokens"] if t["active"])
        print(f"  {r['id']}  {r['name']:<24} {r['status']:<9} {creds} credential(s)  {len(r['grants'])} grant(s)")


def _find_agent(c, ref):
    rows = _call(c, "GET", "/v1/agents")
    hit = [r for r in rows if ref in (r["id"], r["name"]) and r["status"] == "active"] or [r for r in rows if ref in (r["id"], r["name"])]
    if not hit:
        sys.exit(f"no agent named '{ref}'")
    return hit[0]


def cmd_agent_kill(a):
    c = _admin_client(_settings(a.data))
    ag = _find_agent(c, a.name)
    out = _call(c, "POST", f"/v1/agents/{ag['id']}/kill")
    print(f"  killed {out['count']} agent(s) in {out['elapsed_ms']} ms: {ag['name']}"
          + (f" and {out['count'] - 1} helper(s) it delegated" if out["count"] > 1 else ""))
    print(_c("2", "  credentials, grants and leases are revoked; the next request from any of them gets 401"))


def cmd_agent_budget(a):
    c = _admin_client(_settings(a.data))
    ag = _find_agent(c, a.name)
    body = {}
    if a.clear:
        body["budget"] = {}
    elif any(v is not None for v in (a.writes, a.bytes, a.proposals)):
        from agentcloud_server.cli import parse_size
        body["budget"] = {k: v for k, v in (("max_writes", a.writes), ("max_bytes", parse_size(a.bytes) if a.bytes else None),
                                            ("max_proposals", a.proposals)) if v is not None}
    if a.reset:
        body["reset_usage"] = True
    out = _call(c, "PATCH", f"/v1/agents/{ag['id']}", json=body) if body else ag
    print(f"  {out['name']}  budget: {out.get('budget') or 'none'}  used: {out.get('usage')}")


def cmd_approvals(a):
    s = _settings(a.data)
    c = _admin_client(s)
    if a.action in ("approve", "reject"):
        if not a.id:
            sys.exit(f"usage: agentcloud approvals {a.action} <proposal_id>")
        out = _call(c, "POST", f"/v1/approvals/{a.id}/{a.action}", json={"note": a.note or "", "force": a.force})
        print(f"  {out['status']}  {a.id}"); return
    rows = _call(c, "GET", "/v1/approvals")
    if not rows:
        print("  nothing waiting for review"); return
    for r in rows:
        print(f"  {r['id']}  {r['op']:<6} /{r['path']}  in {r['workspace_name']}  by {r['actor']}  {('“' + r['message'] + '”') if r['message'] else ''}")
    print(_c("2", "\n  agentcloud approvals approve <id>   |   agentcloud approvals reject <id>"))


def cmd_webhook(a):
    s = _settings(a.data)
    c = _admin_client(s)
    if a.action == "add":
        if not a.target:
            sys.exit("usage: agentcloud webhook add <url> [--events approval.*,file.deleted]")
        out = _call(c, "POST", "/v1/webhooks", json={"url": a.target, "events": [e.strip() for e in a.events.split(",")]})
        print(f"  {out['id']}  {out['url']}  events={out['events']}\n  secret {_c('33', out['secret'])}  {_c('2', '(shown once)')}")
    elif a.action == "test":
        print(json.dumps(_call(c, "POST", f"/v1/webhooks/{a.target}/test")))
    elif a.action == "rm":
        _call(c, "DELETE", f"/v1/webhooks/{a.target}"); print("  removed")
    else:
        for h in _call(c, "GET", "/v1/webhooks"):
            last = h["last_delivery"] or {}
            print(f"  {h['id']}  {h['url']}  {','.join(h['events'])}  last: {last.get('status', '—')} {last.get('response_code') or ''}")


def cmd_backup(a):
    from . import backup
    s = _settings(a.data)
    dest = Path(a.to or os.environ.get("AGENTCLOUD_BACKUP_DIR") or (s.data_dir.parent / (s.data_dir.name + "-backups"))).expanduser()
    res = backup.create_backup(s.data_dir, dest, include_key=not a.no_key)
    v = backup.verify_backup(Path(res["path"]))
    print(f"  backup   {res['path']}")
    print(f"  content  {res['blobs']} blobs ({res['blob_bytes'] / 1e6:.1f} MB; {res['linked']} hard-linked, {res['copied']} copied) + database")
    print(f"  verified {'ok' if v['ok'] else 'FAILED'}: database integrity {'ok' if v['database_ok'] else 'BAD'}, "
          f"{v['blobs_checked']} blobs {'decrypted' if v['decrypted'] else 'present'}, {len(v['missing'])} missing, {len(v['corrupt'])} corrupt")
    if res["includes_key"]:
        print(_c("33", "  note     the backup contains master.key: anyone with this folder can read your files. Keep it private."))
    if not v["ok"]:
        sys.exit(1)


def cmd_restore(a):
    from . import backup
    s = _settings(a.data) if a.data else None
    target = s.data_dir if s else _settings(None).data_dir
    v = backup.verify_backup(Path(a.source))
    if not v["ok"] and not a.force:
        sys.exit(f"backup failed verification ({len(v['missing'])} missing, {len(v['corrupt'])} corrupt); pass --force to restore anyway")
    res = backup.restore_backup(Path(a.source), target, force=a.force)
    print(f"  restored {res['blobs']} blobs + database into {res['restored_to']}  (stop the server first; start it again with agentcloud up)")


def cmd_status(a):
    s = _settings(a.data)
    info = _call(_admin_client(s), "GET", "/v1/system")
    if a.json:
        print(json.dumps(info, indent=2)); return
    print(f"  up {info['uptime_seconds']}s · v{info['version']} · python {info['python']} · {_url(s)}")
    print(f"  db {info['database_bytes'] / 1e6:.1f} MB · blobs {info['blob_bytes'] / 1e6:.1f} MB · data {info['data_dir']}")
    print("  " + " · ".join(f"{k} {v}" for k, v in info["counts"].items()) + f" · webhooks pending {info['webhooks_pending']}")


def parse_size(v: str) -> int:
    import re
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B?)\s*", v.upper())
    if not m:
        raise SystemExit(f"can't parse size {v!r}; use e.g. 500MB or 2GB")
    mult = {"": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4}
    return int(float(m.group(1)) * mult[m.group(2)])


def main(argv: list[str] | None = None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["up", *argv]                      # `agentcloud --data X` keeps working
    if argv[0] == "serve":
        argv[0] = "up"
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=argparse.SUPPRESS, help="data directory (default ~/.agentcloud, or $AGENTCLOUD_DATA)")
    ap = argparse.ArgumentParser(prog="agentcloud", description="Self-hosted cloud workspaces for AI agents.", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)  # --data works before or after the subcommand
    p = sub.add_parser("up", help="start the server")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--workers", default="auto", help="HTTP processes: auto (min(4, CPUs)) or a number")
    p.add_argument("--no-worker", action="store_true", help="don't run maintenance + webhooks in this process")
    p.set_defaults(fn=cmd_up)
    p = sub.add_parser("worker", help="run maintenance + webhook delivery"); p.add_argument("--once", action="store_true"); p.set_defaults(fn=cmd_worker)
    sub.add_parser("login-link", help="print a one-time dashboard login link").set_defaults(fn=cmd_login_link)
    ag = sub.add_parser("agent", help="manage agents").add_subparsers(dest="sub", required=True)
    _agadd = ag.add_parser
    ag.add_parser = lambda *x, **k: _agadd(*x, parents=[common], **k)
    p = ag.add_parser("create", help="agent + workspace + credential, with connection snippets")
    p.add_argument("name"); p.add_argument("--storage", help="e.g. 500MB, 2GB")
    p.add_argument("--access", default="read_write", choices=["read_only", "read_write", "full"])
    p.add_argument("--approval", default="none", choices=["none", "overwrite", "all"])
    p.add_argument("--client", default="all", choices=["all", "claude", "cursor", "env"]); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_agent_create)
    p = ag.add_parser("list"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_agent_list)
    p = ag.add_parser("kill", help="disable an agent and every helper it delegated, now")
    p.add_argument("name"); p.set_defaults(fn=cmd_agent_kill)
    p = ag.add_parser("budget", help="show or set an agent's write/byte/approval budget")
    p.add_argument("name"); p.add_argument("--writes", type=int); p.add_argument("--bytes", help="e.g. 50MB")
    p.add_argument("--proposals", type=int); p.add_argument("--clear", action="store_true"); p.add_argument("--reset", action="store_true")
    p.set_defaults(fn=cmd_agent_budget)
    p = sub.add_parser("approvals", help="review pending agent changes")
    p.add_argument("action", nargs="?", default="list", choices=["list", "approve", "reject"]); p.add_argument("id", nargs="?")
    p.add_argument("--note"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_approvals)
    p = sub.add_parser("webhook", help="manage event webhooks")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "test", "rm"]); p.add_argument("target", nargs="?")
    p.add_argument("--events", default="*"); p.set_defaults(fn=cmd_webhook)
    p = sub.add_parser("status", help="health and instance facts"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("backup", help="consistent snapshot of database + files (safe while running)")
    p.add_argument("--to", help="backup folder (default $AGENTCLOUD_BACKUP_DIR or <data>-backups)"); p.add_argument("--no-key", action="store_true",
                   help="leave master.key out (you must then keep the key safe separately)")
    p.set_defaults(fn=cmd_backup)
    p = sub.add_parser("restore", help="restore a backup into the data directory")
    p.add_argument("source"); p.add_argument("--force", action="store_true", help="replace a non-empty data dir (old one is moved aside)")
    p.set_defaults(fn=cmd_restore)
    a = ap.parse_args(argv)
    if not hasattr(a, "data"):
        a.data = os.environ.get("AGENTCLOUD_DATA")
    a.fn(a)


if __name__ == "__main__":
    main()
