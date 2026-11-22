"""The agent's view of Agent Cloud.

Principle: an agent should be able to use Agent Cloud without understanding Agent
Cloud. It has a home workspace that behaves like a filesystem rooted at "/", may have
workspaces other agents shared with it (addressed by name), and can ask what it is
and what it may do. Everything here goes through the same permission-checked service
functions as the REST API.
"""
from __future__ import annotations

import base64
import re

from . import service as S
from .auth import Principal, active_grants, rights_for
from .db import now
from .paths import normalize, under
from .service import ApiError, Ctx

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
            ".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".sh", ".toml", ".ini", ".cfg", ".log", ".rst", ".tex", ".css", ".svg"}
SEARCH_MAX_FILE = 2 * 1024 * 1024       # don't scan files bigger than this
SEARCH_MAX_TOTAL = 64 * 1024 * 1024     # stop scanning after this many bytes per query
READ_MAX = 1024 * 1024                  # content returned to an agent in one read


def is_texty(path: str, ctype: str | None) -> bool:
    if ctype and (ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/x-ndjson",
                                                          "image/svg+xml", "application/javascript", "application/sql")):
        return True
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in TEXT_EXT


# ---------------------------------------------------------------- environment discovery

def reachable_workspaces(ctx: Ctx, p: Principal) -> list[dict]:
    if p.is_user:
        rows = ctx.db.all("SELECT * FROM workspaces WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at", p.owner_id)
    else:
        rows = ctx.db.all("""SELECT DISTINCT w.* FROM workspaces w JOIN grants g ON g.workspace_id=w.id
                             WHERE g.agent_id=? AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > ?)
                             AND w.deleted_at IS NULL ORDER BY w.created_at""", p.id, now())
    return [dict(r) for r in rows]


def _caps(ctx: Ctx, p: Principal, ws: dict) -> dict:
    if p.is_user:
        return {"read": True, "write": True, "delete": True, "share": True, "restore": True, "paths": ["/"],
                "approval": "none", "expires_at": None}
    gs = active_grants(ctx.db, p.id, ws["id"])
    c = {"read": any(g["can_read"] for g in gs), "write": any(g["can_write"] for g in gs),
         "delete": any(g["can_delete"] for g in gs), "share": any(g["can_share"] for g in gs)}
    for k in ("read", "write", "delete", "share"):
        c[k] = c[k] and k in p.scopes  # the credential's scopes narrow the grants
    c["restore"] = c["write"]
    c["paths"] = sorted({"/" + g["path_prefix"] for g in gs})
    writing = [g["approval"] for g in gs if g["can_write"] or g["can_delete"]]
    order = {"none": 0, "overwrite": 1, "all": 2}
    c["approval"] = min(writing, key=order.get) if writing else None
    exps = [g["expires_at"] for g in gs]
    c["expires_at"] = None if (not exps or None in exps) else max(exps)
    return c


def home_workspace(ctx: Ctx, p: Principal, wss: list[dict] | None = None) -> dict | None:
    """The workspace an agent's bare paths refer to: its only workspace, or the only one
    where it can write anywhere ('/' with write)."""
    wss = wss if wss is not None else reachable_workspaces(ctx, p)
    if len(wss) == 1:
        return wss[0]
    if p.is_user:
        return None
    row = ctx.db.one("SELECT home_workspace_id FROM agents WHERE id=?", p.id)
    if row and row["home_workspace_id"]:
        for w in wss:
            if w["id"] == row["home_workspace_id"]:
                return w
    full = [w for w in wss if any(g["path_prefix"] == "" and g["can_write"] for g in active_grants(ctx.db, p.id, w["id"]))]
    return full[0] if len(full) == 1 else None


def environment(ctx: Ctx, p: Principal) -> dict:
    """Agent Environment Discovery: who am I, where are my files, what may I do."""
    wss = reachable_workspaces(ctx, p)
    home = home_workspace(ctx, p, wss)
    out_ws = []
    for w in wss:
        caps = _caps(ctx, p, w)
        out_ws.append({
            "id": w["id"], "name": w["name"],
            "role": "home" if home and w["id"] == home["id"] else "shared with you",
            "storage_used_bytes": S.usage_bytes(ctx.db, w["id"]), "storage_limit_bytes": w["quota_bytes"],
            "capabilities": caps,
        })
    env = {
        "identity": {"type": p.kind, "id": p.id, "name": p.name},
        "home_workspace": home["id"] if home else None,
        "workspaces": out_ws,
        "version_history": "every write keeps the previous version; use cloud_history and cloud_restore",
        "persistence": "files persist across sessions, restarts and runtimes; the same credential reaches the same files",
    }
    if not home and len(wss) > 1:
        env["note"] = "more than one workspace: pass `workspace` (name or id) to tools"
    return env


def resolve_workspace(ctx: Ctx, p: Principal, ref: str | None) -> dict:
    wss = reachable_workspaces(ctx, p)
    if not ref:
        home = home_workspace(ctx, p, wss)
        if home:
            return home
        if not wss:
            raise ApiError(404, "no_workspace", "you don't have access to any workspace yet")
        names = ", ".join(f"{w['name']} ({w['id']})" for w in wss)
        raise ApiError(400, "workspace_required", f"you can reach several workspaces; pass workspace=one of: {names}")
    hits = [w for w in wss if w["id"] == ref] or [w for w in wss if w["name"] == ref]
    if len(hits) != 1:
        names = ", ".join(f"{w['name']} ({w['id']})" for w in wss) or "none"
        raise ApiError(404, "workspace_not_found",
                       f"no workspace '{ref}' you can reach" + (" (name is ambiguous, use the id)" if len(hits) > 1 else "")
                       + f"; yours: {names}")
    return hits[0]


# ---------------------------------------------------------------- continuity

CHANGE_ACTIONS = ("create", "update", "delete", "restore", "move", "copy", "approve", "reject")


def start_session(ctx: Ctx, p: Principal) -> float | None:
    """Mark a new agent session; returns when the previous one started (None = first ever)."""
    if p.is_user:
        return None
    row = ctx.db.one("SELECT last_seen_at FROM agents WHERE id=?", p.id)
    prev = row["last_seen_at"] if row else None
    ctx.db.exec("UPDATE agents SET prev_seen_at=?, last_seen_at=? WHERE id=?", prev, now(), p.id)
    return prev


def changes_since(ctx: Ctx, p: Principal, since: float, limit: int = 20) -> dict:
    """What happened that this agent should know about: other actors' changes to files it
    can read, decisions on its approval requests, and access newly shared with it."""
    from .service import flush_activity
    flush_activity(ctx.db)
    wss = {w["id"]: w for w in reachable_workspaces(ctx, p)}
    others = []
    if wss:
        marks = ",".join("?" * len(wss))
        acts = ",".join("?" * len(CHANGE_ACTIONS))
        for r in ctx.db.all(f"""SELECT * FROM activity WHERE workspace_id IN ({marks}) AND action IN ({acts}) AND actor != ?
                                AND created_at > ? ORDER BY id DESC LIMIT 200""", *wss, *CHANGE_ACTIONS, p.actor, since):
            if r["path"] and not rights_for(ctx.db, p, wss[r["workspace_id"]], r["path"]).read:
                continue
            others.append({"workspace": wss[r["workspace_id"]]["name"], "path": "/" + (r["path"] or ""), "action": r["action"],
                           "by": _actor_label(ctx, r["actor"]), "at": r["created_at"]})
            if len(others) >= limit:
                break
    decided = [dict(r) for r in ctx.db.all("""SELECT id, path, op, status, decision_note, decided_at FROM proposals
                                               WHERE actor=? AND decided_at > ? ORDER BY decided_at DESC LIMIT ?""", p.actor, since, limit)]
    pending = ctx.db.one("SELECT COUNT(*) n FROM proposals WHERE actor=? AND status='pending'", p.actor)["n"]
    access = []
    for g in ctx.db.all("""SELECT g.*, w.name AS ws_name FROM grants g JOIN workspaces w ON w.id=g.workspace_id
                           WHERE g.agent_id=? AND g.created_at > ? AND g.revoked_at IS NULL AND g.effect='allow' ORDER BY g.created_at DESC""",
                        p.id, since):
        access.append({"workspace": g["ws_name"], "path": "/" + g["path_prefix"], "by": _actor_label(ctx, g["created_by"]),
                       "rights": [k for k in ("read", "write", "delete", "share") if g[f"can_{k}"]], "note": g["note"]})
    return {"since": since, "changes_by_others": others, "your_requests_decided": decided,
            "your_requests_pending": pending, "new_access": access}


def _actor_label(ctx: Ctx, actor: str) -> str:
    kind, _, ident = actor.partition(":")
    if kind == "agent":
        r = ctx.db.one("SELECT name FROM agents WHERE id=?", ident)
        return r["name"] if r else ident
    return {"user": "a human", "link": "a share link", "system": "the system"}.get(kind, actor)


def briefing_text(ch: dict, max_lines: int = 8) -> str:
    """A compact natural-language summary for the session handshake."""
    out = []
    for a in ch["new_access"][:3]:
        out.append(f"- {a['by']} gave you {'/'.join(a['rights'])} access to {a['path']} in '{a['workspace']}'"
                   + (f': "{a["note"]}"' if a["note"] else ""))
    for d in ch["your_requests_decided"][:3]:
        out.append(f"- Your change to /{d['path']} was {d['status']}" + (f': "{d["decision_note"]}"' if d["decision_note"] else ""))
    others = ch["changes_by_others"]
    for c in others[:max(0, max_lines - len(out))]:
        verb = {"create": "created", "update": "edited", "delete": "deleted", "restore": "restored", "move": "moved",
                "copy": "copied", "approve": "approved a change to", "reject": "rejected a change to"}.get(c["action"], c["action"])
        out.append(f"- {c['by']} {verb} {c['path']} ('{c['workspace']}')")
    if len(others) > max_lines:
        out.append(f"- …and {len(others) - max_lines} more (cloud_changes for details)")
    if ch["your_requests_pending"]:
        out.append(f"- {ch['your_requests_pending']} of your changes still await human approval")
    return "\n".join(out)


# ---------------------------------------------------------------- search

class _TextCache:
    """LRU of decrypted, lowercased text keyed by (org, sha). Blobs are immutable, so
    entries never go stale; the byte budget bounds memory."""

    def __init__(self, budget: int = 64 * 1024 * 1024):
        import collections, threading
        self.budget, self.size = budget, 0
        self.items: "collections.OrderedDict[tuple, tuple[str, str]]" = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, ctx: Ctx, ns: str, sha: str) -> tuple[str, str] | None:
        key = (ns, sha)
        with self.lock:
            hit = self.items.get(key)
            if hit:
                self.items.move_to_end(key)
                return hit
        try:
            text = ctx.blobs.open(ns, sha).decode("utf-8", errors="replace")
        except Exception:
            return None
        val = (text, text.lower(), _chunk_index(text))
        with self.lock:
            self.items[key] = val
            self.size += len(text) * 4   # text + lowercase copy + chunk index
            while self.size > self.budget and self.items:
                _, ev = self.items.popitem(last=False)
                self.size -= len(ev[0]) * 4
        return val


TEXT_CACHE = _TextCache()

def _chunk_index(text: str) -> list[tuple]:
    """Per-file search index, built once per immutable blob: 40-line chunks with term counts."""
    import collections
    lines = text.splitlines()
    out = []
    for i in range(0, max(len(lines), 1), CHUNK_LINES):
        block = lines[i:i + CHUNK_LINES]
        toks = _terms(" ".join(block))
        out.append((i + 1, i + len(block), collections.Counter(toks), len(toks), "\n".join(block)))
    return out


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", q.lower()) if t]


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    pos = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
    start = max(0, pos - width // 3)
    s = text[start:start + width].replace("\n", " ").strip()
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


CHUNK_LINES = 40


def search(ctx: Ctx, p: Principal, query: str, workspaces: list[dict], prefix: str = "", limit: int = 20) -> list[dict]:
    """Ranked search over file names and text content, by chunk.

    Files are split into 40-line chunks and scored with BM25 (k1=1.2, b=0.75); a file's
    score is its best chunk plus a bonus for terms in its path. A result must cover at least
    60% of the query's distinct terms. Each hit says which lines matched, so an agent can
    read just that range next. Content is decrypted in memory only (cached); no plaintext
    index is written to disk."""
    import math
    terms = list(dict.fromkeys(_terms(query)))
    if not terms:
        raise ApiError(400, "bad_query", "search query is empty")
    need = max(1, math.ceil(len(terms) * 0.6))
    docs, scanned = [], 0
    for ws in workspaces:
        for e in S.list_files(ctx, p, ws, prefix, recursive=True):
            if e["kind"] != "file":
                continue
            path_terms = set(_terms(e["path"]))
            chunks: list[tuple[int, int, list[str], str]] = []
            if is_texty(e["path"], e["content_type"]) and e["size"] <= SEARCH_MAX_FILE and scanned < SEARCH_MAX_TOTAL and e["sha256"]:
                scanned += e["size"]
                hit = TEXT_CACHE.get(ctx, ws["owner_id"], e["sha256"])
                if hit:
                    chunks = hit[2]
            covered = {t for t in terms if t in path_terms or any(t in c[2] for c in chunks) or t in e["path"].lower()}
            if len(covered) < need:
                continue
            docs.append((ws, e, path_terms, chunks, covered))
    if not docs:
        return []
    all_chunks = [c for d in docs for c in d[3]] or [(0, 0, {}, 0, "")]
    n = len(all_chunks)
    avg = sum(c[3] for c in all_chunks) / n or 1
    df = {t: sum(1 for c in all_chunks if t in c[2]) for t in terms}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms}
    results = []
    for ws, e, path_terms, chunks, covered in docs:
        best, best_chunk = 0.0, None
        for c in chunks:
            tf_len = c[3] or 1
            sc = 0.0
            for t in terms:
                f = c[2].get(t, 0)
                if f:
                    sc += idf[t] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * tf_len / avg))
            if sc > best:
                best, best_chunk = sc, c
        name_bonus = sum(idf[t] * 1.5 for t in terms if t in path_terms or t in e["path"].lower())
        score = best + name_bonus
        snippet, lines = None, None
        if best_chunk:
            lines = f"{best_chunk[0]}-{best_chunk[1]}"
            snippet = _snippet(best_chunk[4], terms)
        results.append({
            "workspace": ws["name"], "workspace_id": ws["id"], "path": "/" + e["path"], "version": e["version"],
            "size": e["size"], "updated_at": e["updated_at"], "score": round(score, 3), "lines": lines,
            "matched_in": "+".join(k for k, ok in (("name", name_bonus > 0), ("content", best > 0)) if ok),
            "snippet": snippet,
        })
    results.sort(key=lambda r: (-r["score"], -(r["updated_at"] or 0)))
    return results[:limit]


# ---------------------------------------------------------------- content helpers

def decode_content(content: str, encoding: str) -> bytes:
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except Exception:
            raise ApiError(400, "bad_base64", "content is not valid base64")
    if encoding != "utf-8":
        raise ApiError(400, "bad_encoding", "encoding must be 'utf-8' or 'base64'")
    return content.encode("utf-8")


def encode_for_agent(path: str, ctype: str | None, data: bytes) -> tuple[str, str, bool]:
    """Return (content, encoding, truncated)."""
    truncated = len(data) > READ_MAX
    chunk = data[:READ_MAX]
    if is_texty(path, ctype) or b"\x00" not in chunk[:8192]:
        try:
            return chunk.decode("utf-8"), "utf-8", truncated
        except UnicodeDecodeError:
            pass
    return base64.b64encode(chunk).decode(), "base64", truncated


def scoped_paths_hint(ctx: Ctx, p: Principal, ws: dict, path: str) -> str:
    """When an agent is refused, say where it *can* work, so it can recover on its own."""
    if p.is_user:
        return ""
    caps = _caps(ctx, p, ws)
    ok = [x for x in caps["paths"]]
    return f" You can work under: {', '.join(ok)} in '{ws['name']}'." if ok else ""


def readable(ctx: Ctx, p: Principal, ws: dict, path: str) -> bool:
    return rights_for(ctx.db, p, ws, path).read


__all__ = ["environment", "resolve_workspace", "search", "decode_content", "encode_for_agent", "home_workspace",
           "reachable_workspaces", "scoped_paths_hint", "is_texty", "normalize", "under"]
