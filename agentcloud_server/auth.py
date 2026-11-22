"""Identity, credentials and the permission engine.

Trust model (spec §3, §7, Decision 4): an agent never inherits anyone's rights. A
user acts within an organisation; an agent belongs to one organisation and can only do
what explicit, unexpired, unrevoked grants allow, narrowed further by its credential's
scopes and by any deny rule covering the path. All of this is decided server-side.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import Database, now
from .paths import under

_ph = PasswordHasher()

USER_SESSION_PREFIX = "acs_"
USER_KEY_PREFIX = "acu_"
AGENT_TOKEN_PREFIX = "aca_"      # long-lived agent credential
ACCESS_TOKEN_PREFIX = "acx_"     # short-lived token exchanged for a credential
ACTIONS = ("read", "write", "delete", "share")
APPROVAL_RANK = {"none": 0, "overwrite": 1, "all": 2}

# Workspace roles (spec §4.3) as grant presets: read, write, delete, share
ROLES = {
    "owner": (True, True, True, True),
    "admin": (True, True, True, True),
    "editor": (True, True, False, False),
    "viewer": (True, False, False, False),
    "restricted": (False, False, False, False),
}

_access_key: bytes = b""
_last_touch: dict[str, float] = {}
TOUCH_EVERY = 60.0


def _touch(db: Database, table: str, token_id: str, t: float) -> None:
    """Record last_used_at at most once a minute per credential: a write on every
    request would serialise all traffic behind SQLite's single writer."""
    if t - _last_touch.get(token_id, 0) < TOUCH_EVERY:
        return
    _last_touch[token_id] = t
    if len(_last_touch) > 50_000:
        _last_touch.clear()
    db.exec(f"UPDATE {table} SET last_used_at = ? WHERE id = ?", t, token_id)


def set_access_token_key(master_key: bytes) -> None:
    global _access_key
    _access_key = hmac.new(master_key, b"agentcloud/access-token/v1", hashlib.sha256).digest()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def mint_token(prefix: str) -> tuple[str, str, str]:
    """Return (token, sha256 hash to store, display prefix). Tokens carry 256 bits of
    entropy, so a fast hash is right for lookup; lookup is by hash, so there is no
    secret-dependent string comparison to time."""
    token = prefix + secrets.token_urlsafe(32)
    return token, hash_token(token), token[: len(prefix) + 6]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_access_token(agent_id: str, credential_id: str, ttl: int) -> tuple[str, float]:
    exp = now() + ttl
    payload = _b64(json.dumps({"sub": agent_id, "cred": credential_id, "exp": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_access_key, payload.encode(), hashlib.sha256).digest())
    return f"{ACCESS_TOKEN_PREFIX}{payload}.{sig}", exp


def _verify_access_token(token: str) -> dict | None:
    try:
        payload, sig = token[len(ACCESS_TOKEN_PREFIX):].split(".", 1)
        good = _b64(hmac.new(_access_key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        claims = json.loads(_unb64(payload))
        return claims if claims.get("exp", 0) > now() else None
    except Exception:
        return None


@dataclass
class Principal:
    kind: str                  # 'user' | 'agent'
    id: str
    owner_id: str              # organisation id
    name: str = ""
    token_id: str | None = None
    scopes: frozenset = field(default_factory=lambda: frozenset(ACTIONS))
    org_role: str | None = None   # for users: owner/admin/member

    @property
    def actor(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


def user_orgs(db: Database, user_id: str):
    return db.all("""SELECT o.*, m.role FROM organisation_members m JOIN organisations o ON o.id=m.organisation_id
                     WHERE m.user_id=? ORDER BY m.created_at""", user_id)


def authenticate(db: Database, token: str | None, org_hint: str | None = None) -> Principal | None:
    if not token:
        return None
    t = now()
    if token.startswith(ACCESS_TOKEN_PREFIX):
        claims = _verify_access_token(token)
        if not claims:
            return None
        # Revocation still applies immediately: the credential behind the token must be live.
        row = db.one("""SELECT t.id AS tid, t.scopes, a.id, a.owner_id, a.name FROM agent_tokens t JOIN agents a ON a.id=t.agent_id
                        LEFT JOIN agents pa ON pa.id=a.parent_agent_id
                        WHERE t.id=? AND a.id=? AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > ?)
                        AND a.disabled_at IS NULL AND pa.disabled_at IS NULL""", claims["cred"], claims["sub"], t)
        if not row:
            return None
        return Principal("agent", row["id"], row["owner_id"], row["name"], row["tid"], frozenset(json.loads(row["scopes"])))
    th = hash_token(token)
    if token.startswith(AGENT_TOKEN_PREFIX):
        row = db.one(
            """SELECT t.id AS tid, t.scopes, a.id, a.owner_id, a.name FROM agent_tokens t
               JOIN agents a ON a.id = t.agent_id LEFT JOIN agents pa ON pa.id = a.parent_agent_id
               WHERE t.token_hash = ? AND t.revoked_at IS NULL
                 AND (t.expires_at IS NULL OR t.expires_at > ?) AND a.disabled_at IS NULL AND pa.disabled_at IS NULL""",
            th, t,
        )
        if not row:
            return None
        _touch(db, "agent_tokens", row["tid"], t)
        return Principal("agent", row["id"], row["owner_id"], row["name"], row["tid"], frozenset(json.loads(row["scopes"])))
    if token.startswith((USER_SESSION_PREFIX, USER_KEY_PREFIX)):
        row = db.one(
            """SELECT t.id AS tid, u.id, u.email FROM user_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token_hash = ? AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > ?)""",
            th, t,
        )
        if not row:
            return None
        orgs = user_orgs(db, row["id"])
        if not orgs:
            return None
        org = next((o for o in orgs if o["id"] == org_hint), None) if org_hint else orgs[0]
        if org is None:
            return None  # asked for an organisation they aren't in
        _touch(db, "user_tokens", row["tid"], t)
        return Principal("user", row["id"], org["id"], row["email"], row["tid"], org_role=org["role"])
    return None


@dataclass
class Rights:
    read: bool = False
    write: bool = False
    delete: bool = False
    share: bool = False
    # Approval policy from the least restrictive allow-grant that permits the action.
    write_approval: str = "none"
    delete_approval: str = "none"


def active_grants(db: Database, agent_id: str, workspace_id: str, effect: str | None = "allow"):
    sql = """SELECT * FROM grants WHERE agent_id = ? AND workspace_id = ? AND revoked_at IS NULL
             AND (expires_at IS NULL OR expires_at > ?)"""
    args = [agent_id, workspace_id, now()]
    if effect:
        sql += " AND effect = ?"
        args.append(effect)
    return db.all(sql, *args)


def _denied(db: Database, agent_id: str, ws_id: str, path: str) -> set[str]:
    """Rights removed by deny rules covering `path`. Denying read removes everything."""
    out: set[str] = set()
    for g in active_grants(db, agent_id, ws_id, "deny"):
        if under(path, g["path_prefix"]):
            if g["can_read"]:
                out.update(ACTIONS)
            for a in ("write", "delete", "share"):
                if g[f"can_{a}"]:
                    out.add(a)
    return out


def rights_for(db: Database, p: Principal, ws: dict, path: str) -> Rights:
    """Effective rights of principal `p` on `path` in workspace `ws`:
    (union of allow grants covering path) - (deny rules covering path) ∩ (credential scopes)."""
    if ws["owner_id"] != p.owner_id:
        return Rights()
    if p.is_user:
        return Rights(True, True, True, True)
    r = Rights()
    best_w = best_d = None
    grants = active_grants(db, p.id, ws["id"], effect=None)
    denied: set[str] = set()
    for g in grants:
        if g["effect"] == "deny" and under(path, g["path_prefix"]):
            if g["can_read"]:
                denied.update(ACTIONS)
            denied.update(a for a in ("write", "delete", "share") if g[f"can_{a}"])
    for g in grants:
        if g["effect"] != "allow" or not under(path, g["path_prefix"]):
            continue
        r.read |= bool(g["can_read"])
        r.share |= bool(g["can_share"])
        if g["can_write"]:
            r.write = True
            if best_w is None or APPROVAL_RANK[g["approval"]] < APPROVAL_RANK[best_w]:
                best_w = g["approval"]
        if g["can_delete"]:
            r.delete = True
            if best_d is None or APPROVAL_RANK[g["approval"]] < APPROVAL_RANK[best_d]:
                best_d = g["approval"]
    r.write_approval = best_w or "none"
    r.delete_approval = best_d or "none"
    for a in denied | (set(ACTIONS) - set(p.scopes)):
        setattr(r, a, False)
    return r


def readable_prefixes(db: Database, p: Principal, ws: dict) -> list[str] | None:
    """Prefixes the principal may read (deny rules applied per file by the caller). None = everything."""
    if ws["owner_id"] != p.owner_id:
        return []
    if p.is_user:
        return None
    if "read" not in p.scopes:
        return []
    return [g["path_prefix"] for g in active_grants(db, p.id, ws["id"]) if g["can_read"]]


def deny_read_prefixes(db: Database, p: Principal, ws: dict) -> list[str]:
    if p.is_user:
        return []
    return [g["path_prefix"] for g in active_grants(db, p.id, ws["id"], "deny") if g["can_read"]]


def share_limits(db: Database, p: Principal, ws: dict, prefix: str):
    """For an agent re-sharing `prefix`, the ceiling it may grant: one of its own allow
    grants with can_share that covers the whole prefix. None if no such grant, if the
    credential lacks the share scope, or if a deny rule removes share there."""
    if "share" not in p.scopes or "share" in _denied(db, p.id, ws["id"], prefix):
        return None
    best = None
    for g in active_grants(db, p.id, ws["id"]):
        if not g["can_share"]:
            continue
        gp = g["path_prefix"]
        covers = gp == "" or (gp.endswith("/") and prefix.startswith(gp)) or gp == prefix
        if not covers:
            continue
        score = (g["can_read"] + g["can_write"] + g["can_delete"], -APPROVAL_RANK[g["approval"]])
        if best is None or score > best[0]:
            best = (score, g)
    return best[1] if best else None
