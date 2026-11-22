"""Agent self-governance.

At a million agents, humans can't approve every share or review every change. So each
agent owns its workspaces and governs them itself: it decides who else can read or
write, sets deny rules, reviews other agents' proposed changes, and creates new isolated
workspaces out of its own storage. Humans set one organisation-wide constitution (with
safe defaults, so they don't have to touch it) and step in only by exception.

What keeps this safe when agents can be tricked:
- The constitution caps what any agent can grant (rights, duration, scope).
- An agent that has recently read untrusted content can't widen anyone's access, so a
  poisoned web page can't talk an agent into giving its workspace away.
- No self-approval: an agent can't approve its own proposals.
- Everything is audited, and the kill switch still works on owners.
"""
from __future__ import annotations

import json

from .auth import Principal
from .db import Database, now

DEFAULT_POLICY = {
    "agents_govern": True,             # owner agents may grant, revoke and set deny rules in their workspaces
    "agent_review": True,              # owner agents may approve or reject other agents' proposals
    "agents_create_workspaces": True,  # owner agents may carve new isolated workspaces from their own quota
    "grantable": ["read", "write", "share"],   # rights an agent may give others ("delete" is opt-in)
    "max_grant_hours": 720,            # longest grant an agent may issue (None = no limit)
    "cross_project": True,             # may grant to agents in other projects of the organisation
    "untrusted_can_grant": False,      # an agent whose recent reads were untrusted may not widen access
    "max_workspaces_per_agent": 50,
}


def policy(db: Database, org_id: str) -> dict:
    r = db.one("SELECT agent_policy FROM organisations WHERE id=?", org_id)
    p = dict(DEFAULT_POLICY)
    if r and r["agent_policy"]:
        try:
            p.update({k: v for k, v in json.loads(r["agent_policy"]).items() if k in DEFAULT_POLICY})
        except ValueError:
            pass
    return p


def validate(changes: dict) -> dict:
    out = {}
    for k, v in changes.items():
        if k not in DEFAULT_POLICY:
            raise ValueError(f"unknown policy key '{k}' (known: {', '.join(DEFAULT_POLICY)})")
        d = DEFAULT_POLICY[k]
        if k == "grantable":
            if not isinstance(v, list) or any(x not in ("read", "write", "delete", "share") for x in v):
                raise ValueError("grantable must be a list drawn from read, write, delete, share")
        elif k == "max_grant_hours":
            if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                raise ValueError("max_grant_hours must be a positive number or null")
        elif isinstance(d, bool) and not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
        elif isinstance(d, int) and not isinstance(d, bool) and (not isinstance(v, int) or v < 0):
            raise ValueError(f"{k} must be a non-negative integer")
        out[k] = v
    return out


def is_owner(db: Database, p: Principal, ws: dict) -> bool:
    """An agent owns a workspace if it holds the 'owner' role there (live, unexpired)."""
    if p.is_user or ws["owner_id"] != p.owner_id:
        return False
    return bool(db.one("""SELECT 1 FROM grants WHERE workspace_id=? AND agent_id=? AND role='owner' AND effect='allow'
                          AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)""", ws["id"], p.id, now()))


def owned_workspaces(db: Database, p: Principal) -> list[dict]:
    if p.is_user:
        return []
    return [dict(r) for r in db.all("""SELECT w.* FROM workspaces w JOIN grants g ON g.workspace_id=w.id
                                       WHERE g.agent_id=? AND g.role='owner' AND g.effect='allow' AND g.revoked_at IS NULL
                                       AND (g.expires_at IS NULL OR g.expires_at > ?) AND w.deleted_at IS NULL
                                       ORDER BY w.created_at""", p.id, now())]


def recently_untrusted(db: Database, p: Principal) -> list[dict]:
    """Untrusted inputs in this agent's recent read set (same task, when one is set)."""
    from . import provenance as P
    wss = [r["workspace_id"] for r in db.all("SELECT DISTINCT workspace_id FROM grants WHERE agent_id=? AND revoked_at IS NULL", p.id)]
    bad = []
    for fid, n in P.read_set(db, p.actor, wss, P.current().task_id):
        if P.trust_of(db, fid, n) == "untrusted":
            f = db.one("SELECT path FROM files WHERE id=?", fid)
            bad.append({"path": "/" + f["path"] if f else fid, "version": n})
    return bad
