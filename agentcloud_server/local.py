"""Zero-config local mode: the first `agentcloud up` creates an admin, an organisation,
a default project and an admin API key, writes them to <data>/admin.json (mode 0600),
and prints a one-time login link. Nothing to sign up for on your own machine.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .auth import USER_KEY_PREFIX, hash_password, hash_token, mint_token
from .db import Database, new_id, now

MAGIC_TTL = 15 * 60
ADMIN_FILE = "admin.json"


def admin_file(data_dir: Path) -> Path:
    return data_dir / ADMIN_FILE


def load_admin(data_dir: Path) -> dict | None:
    p = admin_file(data_dir)
    return json.loads(p.read_text()) if p.exists() else None


def bootstrap(db: Database, data_dir: Path) -> dict | None:
    """Create the local admin if the instance has no users yet. Returns the admin
    record (with secrets) only when it was just created."""
    if db.one("SELECT 1 FROM users LIMIT 1"):
        return None
    t = now()
    uid, org, proj = new_id("usr"), new_id("org"), new_id("prj")
    password = secrets.token_urlsafe(18)
    key, key_hash, prefix = mint_token(USER_KEY_PREFIX)
    with db.tx():
        db.exec("INSERT INTO users(id,email,password_hash,created_at) VALUES (?,?,?,?)", uid, "admin@localhost", hash_password(password), t)
        db.exec("INSERT INTO organisations(id,name,created_at) VALUES (?,?,?)", org, "Local", t)
        db.exec("INSERT INTO organisation_members(id,organisation_id,user_id,role,created_at) VALUES (?,?,?,?,?)", new_id("mem"), org, uid, "owner", t)
        db.exec("INSERT INTO projects(id,owner_id,name,created_at) VALUES (?,?,?,?)", proj, org, "Default", t)
        db.exec("INSERT INTO user_tokens(id,user_id,kind,name,token_hash,prefix,created_at) VALUES (?,?,?,?,?,?,?)",
                new_id("key"), uid, "api_key", "local-admin", key_hash, prefix, t)
    rec = {"email": "admin@localhost", "password": password, "api_key": key, "user_id": uid, "organisation_id": org}
    p = admin_file(data_dir)
    p.write_text(json.dumps(rec, indent=2))
    os.chmod(p, 0o600)
    return rec


def magic_link(db: Database, user_id: str, base_url: str) -> str:
    code = secrets.token_urlsafe(24)
    db.exec("INSERT INTO magic_links(code_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            hash_token(code), user_id, now(), now() + MAGIC_TTL)
    return f"{base_url.rstrip('/')}/#login={code}"


def redeem(db: Database, code: str) -> str | None:
    """Single use: returns the user id and burns the code, or None."""
    with db.tx():
        row = db.one("SELECT * FROM magic_links WHERE code_hash=? AND used_at IS NULL AND expires_at > ?", hash_token(code), now())
        if not row:
            return None
        db.exec("UPDATE magic_links SET used_at=? WHERE code_hash=?", now(), row["code_hash"])
        return row["user_id"]
