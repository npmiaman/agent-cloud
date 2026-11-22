"""Multi-agent coordination and runaway protection.

Leases: an agent claims a file or folder (`cloud_lease`) so other agents get a clear
"held by X for 4 more minutes" instead of silently overwriting each other. Every lease
gets a fencing token from a per-workspace counter that only increases. A writer that
sends its token is refused if its lease has since expired and someone else took over
(the classic stale-lock bug in long-running agents). Humans are not blocked by leases.

Budgets: per-agent caps on writes, bytes written and approval requests, set by a human
or by the parent agent that delegated the credential. Runaway detection: an agent that
rewrites the same file dozens of times in a few minutes is stopped and a
`agent.runaway` event fires, independent of any budget.
"""
from __future__ import annotations

import json

from .db import Database, new_id, now
from .paths import under

DEFAULT_LEASE_S = 300
MAX_LEASE_S = 24 * 3600
RUNAWAY_WRITES = 60          # versions of one file by one agent ...
RUNAWAY_WINDOW = 600         # ... within 10 minutes, cycling through repeated contents
RUNAWAY_HARD = 300           # any writes to one file within the window


def _err(status: int, code: str, message: str, **extra):
    from .service import ApiError  # late import: service imports this module
    return ApiError(status, code, message, **extra)


def _overlaps(a: str, b: str) -> bool:
    return under(a, b) or under(b, a)


def active(db: Database, ws_id: str, path: str | None = None) -> list:
    rows = db.all("SELECT * FROM leases WHERE workspace_id=? AND released_at IS NULL AND expires_at > ? ORDER BY created_at",
                  ws_id, now())
    return [r for r in rows if path is None or _overlaps(path, r["path"])]


def holder_label(db: Database, actor: str) -> str:
    if actor.startswith("agent:"):
        r = db.one("SELECT name FROM agents WHERE id=?", actor[6:])
        return r["name"] if r else actor
    return "a human"


def acquire(db: Database, ws: dict, actor: str, path: str, ttl: int | None, note: str | None) -> dict:
    ttl = max(5, min(int(ttl or DEFAULT_LEASE_S), MAX_LEASE_S))
    with db.tx():
        mine = None
        for r in active(db, ws["id"], path):
            if r["holder"] != actor:
                raise _err(423, "LEASE_HELD",
                                        f"/{r['path']} is leased by {holder_label(db, r['holder'])} for another "
                                        f"{int(r['expires_at'] - now())}s", holder=holder_label(db, r["holder"]),
                                        lease_path="/" + r["path"], expires_in=int(r["expires_at"] - now()))
            if r["path"] == path:
                mine = r
        if mine:  # re-acquiring your own lease renews it and keeps its fence
            db.exec("UPDATE leases SET expires_at=?, note=COALESCE(?, note) WHERE id=?", now() + ttl, note, mine["id"])
            return _out(db.one("SELECT * FROM leases WHERE id=?", mine["id"]))
        fence = (db.one("SELECT MAX(fence) m FROM leases WHERE workspace_id=?", ws["id"])["m"] or 0) + 1
        lid = new_id("lse")
        db.exec("INSERT INTO leases(id, workspace_id, path, holder, fence, note, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                lid, ws["id"], path, actor, fence, (note or "")[:300] or None, now(), now() + ttl)
        return _out(db.one("SELECT * FROM leases WHERE id=?", lid))


def release(db: Database, ws: dict, actor: str, path: str) -> int:
    with db.tx():
        return db.exec("UPDATE leases SET released_at=? WHERE workspace_id=? AND path=? AND holder=? AND released_at IS NULL",
                       now(), ws["id"], path, actor).rowcount


def _out(r) -> dict:
    return {"path": "/" + r["path"], "fence": r["fence"], "expires_at": r["expires_at"],
            "expires_in": max(0, int(r["expires_at"] - now())), "note": r["note"]}


def check_write(db: Database, ws: dict, actor: str, path: str, fence: int | None) -> None:
    """Called before any mutation. Humans pass through; agents are stopped by other
    holders' leases, and a fencing token must still be the current one."""
    if actor.startswith("user:"):
        return
    leases = active(db, ws["id"], path)
    for r in leases:
        if r["holder"] != actor:
            raise _err(423, "LEASE_HELD",
                                    f"/{r['path']} is leased by {holder_label(db, r['holder'])} for another "
                                    f"{int(r['expires_at'] - now())}s; wait, or work elsewhere",
                                    holder=holder_label(db, r["holder"]), lease_path="/" + r["path"],
                                    expires_in=int(r["expires_at"] - now()))
    if fence is not None:
        cur = [r["fence"] for r in leases if r["holder"] == actor]
        if fence not in cur:
            raise _err(409, "STALE_FENCE",
                                    f"fencing token {fence} is no longer valid for /{path}: your lease expired or was "
                                    "released. Re-acquire it and re-read before writing.", current=cur or None)


# ---------------------------------------------------------------- budgets and runaway detection

def budget_of(db: Database, agent_id: str) -> tuple[dict, dict]:
    r = db.one("SELECT budget, used_writes, used_bytes, used_proposals FROM agents WHERE id=?", agent_id)
    if not r:
        return {}, {}
    try:
        b = json.loads(r["budget"]) if r["budget"] else {}
    except ValueError:
        b = {}
    return b, {"writes": r["used_writes"], "bytes": r["used_bytes"], "proposals": r["used_proposals"]}


def check_budget(db: Database, agent_id: str, size: int, proposal: bool) -> None:
    b, used = budget_of(db, agent_id)
    if not b:
        return
    if proposal and b.get("max_proposals") is not None and used["proposals"] + 1 > b["max_proposals"]:
        raise _err(429, "BUDGET_EXCEEDED", f"approval-request budget used up ({used['proposals']}/{b['max_proposals']})",
                                budget=b, used=used)
    if b.get("max_writes") is not None and used["writes"] + 1 > b["max_writes"]:
        raise _err(429, "BUDGET_EXCEEDED", f"write budget used up ({used['writes']}/{b['max_writes']} writes)",
                                budget=b, used=used)
    if b.get("max_bytes") is not None and used["bytes"] + size > b["max_bytes"]:
        raise _err(429, "BUDGET_EXCEEDED",
                                f"byte budget would be exceeded ({used['bytes'] + size:,} of {b['max_bytes']:,} bytes)",
                                budget=b, used=used)


def charge(db: Database, agent_id: str, size: int, proposal: bool = False) -> None:
    if proposal:
        db.exec("UPDATE agents SET used_proposals=used_proposals+1 WHERE id=?", agent_id)
    else:
        db.exec("UPDATE agents SET used_writes=used_writes+1, used_bytes=used_bytes+? WHERE id=?", size, agent_id)


def runaway(db: Database, actor: str, file_id: str | None) -> int:
    """Non-zero when this actor looks stuck on this file: within the window, either 60+
    writes that keep cycling through the same contents (A -> B -> A ...), or 300+ writes
    of any kind. Appending to a journal produces new content each time, so it's fine."""
    if not file_id or actor.startswith("user:"):
        return 0
    r = db.one("""SELECT COUNT(*) n, COUNT(DISTINCT blob_sha256) d FROM versions
                  WHERE file_id=? AND actor=? AND created_at > ?""", file_id, actor, now() - RUNAWAY_WINDOW)
    if r["n"] >= RUNAWAY_HARD or (r["n"] >= RUNAWAY_WRITES and r["d"] * 2 <= r["n"]):
        return r["n"]
    return 0


def subtree(db: Database, agent_id: str) -> list[str]:
    """Agent ids delegated (transitively) from agent_id, excluding itself."""
    out, frontier = [], [agent_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        frontier = [r["id"] for r in db.all(f"SELECT id FROM agents WHERE parent_agent_id IN ({marks})", *frontier)
                    if r["id"] not in out]
        out.extend(frontier)
    return out


def kill(db: Database, agent_id: str) -> list[str]:
    """Disable an agent and its whole delegation subtree: credentials, grants and leases
    stop working on the next request (every request re-checks the database)."""
    ids = [agent_id] + subtree(db, agent_id)
    t = now()
    marks = ",".join("?" * len(ids))
    with db.tx():
        db.exec(f"UPDATE agents SET disabled_at=COALESCE(disabled_at, ?) WHERE id IN ({marks})", t, *ids)
        db.exec(f"UPDATE agent_tokens SET revoked_at=? WHERE agent_id IN ({marks}) AND revoked_at IS NULL", t, *ids)
        db.exec(f"UPDATE grants SET revoked_at=? WHERE agent_id IN ({marks}) AND revoked_at IS NULL", t, *ids)
        db.exec(f"UPDATE leases SET released_at=? WHERE holder IN ({','.join('?' * len(ids))}) AND released_at IS NULL",
                t, *[f"agent:{i}" for i in ids])
    return ids
