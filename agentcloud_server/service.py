"""File operations. Every mutation goes through here so versioning, permissions,
approvals, quotas and the audit log can't be skipped by a route (REST, MCP or uploads)."""
from __future__ import annotations

import contextvars
import csv
import threading
import time
import difflib
import io
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from .auth import Principal, Rights, deny_read_prefixes, readable_prefixes, rights_for
from .db import Database, new_id, now
from .paths import normalize, under
from .storage import LocalEncryptedBlobStore
from . import coordination as C
from . import provenance as P

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class ApiError(Exception):
    """An error the API reports. `code` follows the spec's UPPER_SNAKE codes (§11.10)."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code.upper(), message, extra


def forbidden(msg="not permitted"):
    return ApiError(403, "PERMISSION_DENIED", msg)


def not_found(what="not found", code="NOT_FOUND"):
    return ApiError(404, code, what)


def file_not_found(what):
    return ApiError(404, "FILE_NOT_FOUND", what)


def guess_type(path: str, given: str | None) -> str:
    if given and given not in ("application/octet-stream", "application/x-www-form-urlencoded"):
        return given.split(";")[0].strip()
    t, _ = mimetypes.guess_type(path)
    return t or given or "application/octet-stream"


@dataclass
class Ctx:
    db: Database
    blobs: LocalEncryptedBlobStore


# Audit event names (spec §17.4) for internal action names.
EVENT_NAMES = {
    "signup": "user.signed_up", "create_api_key": "api_key.created", "revoke_api_key": "api_key.revoked",
    "create_workspace": "workspace.created", "update_workspace": "workspace.updated", "delete_workspace": "workspace.deleted",
    "create_agent": "agent.created", "provision_agent": "agent.provisioned", "update_agent": "agent.updated",
    "delete_agent": "agent.deleted", "create_agent_token": "credential.created", "revoke_agent_token": "credential.revoked",
    "rotate_agent_token": "credential.rotated", "access_token": "credential.exchanged",
    "create": "file.created", "update": "file.modified", "delete": "file.deleted", "restore": "file.restored",
    "move": "file.moved", "copy": "file.copied", "read": "file.read", "download": "file.downloaded",
    "mkdir": "folder.created", "rmdir": "folder.deleted", "upload_started": "file.upload_started",
    "grant": "permission.changed", "revoke_grant": "permission.changed", "member_set": "permission.changed",
    "member_removed": "permission.changed",
    "propose_write": "approval.created", "propose_delete": "approval.created", "approve": "approval.approved",
    "reject": "approval.rejected", "create_link": "file.shared", "revoke_link": "share.revoked",
    "link_download": "file.downloaded", "search": "file.searched", "purge": "file.purged",
    "create_project": "project.created", "update_project": "project.updated", "delete_project": "project.deleted",
    "add_member": "organisation.member_added", "remove_member": "organisation.member_removed",
    "runaway": "agent.runaway", "kill_agent": "agent.killed", "delegate": "agent.delegated",
    "lease": "lease.acquired", "release_lease": "lease.released", "quarantine": "trust.quarantined",
    "endorse": "trust.endorsed", "set_trust_policy": "workspace.trust_policy_changed",
}


_ACTIVITY_SQL = "INSERT INTO activity(owner_id, workspace_id, actor, action, path, file_id, detail, request_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)"


def log(db: Database, owner_id: str, ws_id: str | None, actor: str, action: str, path: str | None = None,
        file_id: str | None = None, *, buffered: bool = False, **detail):
    """Append to the audit log. Mutations log inside their own transaction (durable with
    the change). High-volume read events pass buffered=True: they're batched and written
    every ~250 ms by one writer instead of forcing a SQLite write per read."""
    task = P.current().task_id
    if task and "task" not in detail:
        detail["task"] = task
    row = (owner_id, ws_id, actor, action, path, file_id, json.dumps(detail), request_id_var.get(), now())
    if buffered:
        buf = _buffer(db)
        with buf["lock"]:
            buf["rows"].append(row)
        return  # high-volume read events: audited, but not sent to webhooks
    db.exec(_ACTIVITY_SQL, *row)
    from . import webhooks
    webhooks.enqueue(db, owner_id, EVENT_NAMES.get(action, action),
                     {"actor": actor, "workspace_id": ws_id, "path": path, "file_id": file_id,
                      "request_id": row[7], "detail": detail})


def _buffer(db: Database) -> dict:
    b = getattr(db, "_activity_buffer", None)
    if b is None:
        b = db._activity_buffer = {"lock": threading.Lock(), "rows": []}
    return b


def flush_activity(db: Database) -> int:
    """Write buffered audit events. Called by the flusher thread, before any audit query,
    and at shutdown, so readers always see a complete log."""
    buf = _buffer(db)
    with buf["lock"]:
        rows, buf["rows"] = buf["rows"], []
    if rows:
        with db.tx() as conn:
            conn.executemany(_ACTIVITY_SQL, rows)
    return len(rows)


def start_activity_flusher(db: Database, interval: float = 0.25) -> None:
    def loop():
        while True:
            time.sleep(interval)
            try:
                flush_activity(db)
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="agentcloud-audit-flush").start()


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- workspaces

def get_workspace(ctx: Ctx, p: Principal, ws_id: str) -> dict:
    ws = ctx.db.one("SELECT * FROM workspaces WHERE id = ? AND deleted_at IS NULL", ws_id)
    # Same 404 whether it doesn't exist or isn't yours: no existence oracle.
    if not ws or ws["owner_id"] != p.owner_id:
        raise not_found("workspace not found", "WORKSPACE_NOT_FOUND")
    ws = dict(ws)
    if not p.is_user:
        from .auth import active_grants
        if not active_grants(ctx.db, p.id, ws_id):
            raise not_found("workspace not found", "WORKSPACE_NOT_FOUND")
    return ws


USAGE_TTL = 5.0


def invalidate_usage(db: Database, ws_id: str) -> None:
    getattr(db, "_usage_cache", {}).pop(ws_id, None)


def usage_bytes(db: Database, ws_id: str, fresh: bool = True) -> int:
    """Committed bytes (distinct content, all versions) + pending proposals + live upload
    reservations. Quota checks always compute it fresh inside their transaction; listings
    pass fresh=False and get a value cached for a few seconds (cleared on every write)."""
    cache = getattr(db, "_usage_cache", None)
    if cache is None:
        cache = db._usage_cache = {}
    if not fresh:
        hit = cache.get(ws_id)
        if hit and time.monotonic() - hit[1] < USAGE_TTL:
            return hit[0]
    val = _usage_bytes(db, ws_id)
    cache[ws_id] = (val, time.monotonic())
    return val


def _usage_bytes(db: Database, ws_id: str) -> int:
    row = db.one(
        """SELECT COALESCE(SUM(size),0) AS s FROM (SELECT DISTINCT v.blob_sha256, v.size FROM versions v
           JOIN files f ON f.id = v.file_id WHERE f.workspace_id = ? AND v.blob_sha256 IS NOT NULL
           UNION SELECT DISTINCT blob_sha256, size FROM proposals WHERE workspace_id = ? AND status='pending' AND blob_sha256 IS NOT NULL)""",
        ws_id, ws_id,
    )
    reserved = db.one("""SELECT COALESCE(SUM(declared_size),0) AS s FROM uploads WHERE workspace_id=?
                         AND state IN ('CREATED','UPLOADED') AND expires_at > ?""", ws_id, now())
    return int(row["s"]) + int(reserved["s"])


def blob_in_workspace(db: Database, ws_id: str, sha: str) -> bool:
    return bool(db.one(
        """SELECT 1 FROM versions v JOIN files f ON f.id=v.file_id WHERE f.workspace_id=? AND v.blob_sha256=?
           UNION SELECT 1 FROM proposals WHERE workspace_id=? AND blob_sha256=? AND status='pending' LIMIT 1""",
        ws_id, sha, ws_id, sha,
    ))


def ensure_quota(db: Database, ws: dict, size: int, sha: str | None = None, exclude_upload: str | None = None):
    if sha and blob_in_workspace(db, ws["id"], sha):
        return  # deduplicated: costs nothing
    used = usage_bytes(db, ws["id"])
    if exclude_upload:
        r = db.one("SELECT declared_size FROM uploads WHERE id=? AND state IN ('CREATED','UPLOADED')", exclude_upload)
        used -= r["declared_size"] if r else 0
    if used + size > ws["quota_bytes"]:
        raise ApiError(507, "QUOTA_EXCEEDED",
                       f"workspace quota {ws['quota_bytes']} bytes; used {used}, this upload {size}",
                       quota_bytes=ws["quota_bytes"], used_bytes=used)


# ---------------------------------------------------------------- lookups

def file_row(db: Database, ws_id: str, path: str):
    """The row at `path`: the live one if any, else the most recently deleted one."""
    return db.one("""SELECT * FROM files WHERE workspace_id = ? AND path = ?
                     ORDER BY (deleted_at IS NULL) DESC, deleted_at DESC LIMIT 1""", ws_id, path)


def live_row(db: Database, ws_id: str, path: str):
    return db.one("SELECT * FROM files WHERE workspace_id=? AND path=? AND deleted_at IS NULL", ws_id, path)


def version_row(db: Database, file_id: str, n: int):
    return db.one("SELECT * FROM versions WHERE file_id = ? AND n = ?", file_id, n)


def file_by_id(ctx: Ctx, p: Principal, file_id: str) -> tuple[dict, dict]:
    """(workspace, file) for a file id, after checking the caller may read it. Unauthorised
    and nonexistent look identical (spec §5.5: knowing an id grants nothing)."""
    f = ctx.db.one("SELECT * FROM files WHERE id=?", file_id)
    if not f:
        raise file_not_found("file not found")
    try:
        ws = get_workspace(ctx, p, f["workspace_id"])
    except ApiError:
        raise file_not_found("file not found")
    if not rights_for(ctx.db, p, ws, f["path"]).read:
        raise file_not_found("file not found")
    return ws, dict(f)


def live_version(f) -> int:
    """Version number an If-Match must name; 0 when the file doesn't currently exist."""
    return 0 if (f is None or f["deleted_at"] is not None) else f["current_version"]


def require(r: Rights, action: str, path: str):
    if not getattr(r, action):
        raise forbidden(f"no {action} permission on '/{path}'")


def file_meta(db: Database, f, v=None) -> dict:
    if v is None and f["current_version"]:
        v = version_row(db, f["id"], f["current_version"])
    return {
        "id": f["id"],
        "workspace_id": f["workspace_id"],
        "path": f["path"],
        "name": f["path"].rstrip("/").rsplit("/", 1)[-1],
        "kind": f["kind"],
        "version": f["current_version"],
        "deleted": f["deleted_at"] is not None,
        "deleted_at": f["deleted_at"],
        "size": v["size"] if v else 0,
        "content_type": v["content_type"] if v else None,
        "sha256": v["blob_sha256"] if v else None,
        "updated_at": f["updated_at"],
        "updated_by": v["actor"] if v else None,
        "created_at": f["created_at"],
        "created_by": f["created_by"],
    }


def read_version(ctx: Ctx, p: Principal, ws: dict, raw_path: str, version: int | None = None, log_it: bool = True):
    """Authorise a read and resolve (file, version). Content is fetched separately so
    downloads can stream."""
    path = normalize(raw_path)
    require(rights_for(ctx.db, p, ws, path), "read", path)
    f = file_row(ctx.db, ws["id"], path)
    if not f or f["kind"] != "file":
        raise file_not_found(f"'/{path}' not found")
    return resolve_version(ctx, p, ws, f, version, log_it)


def resolve_version(ctx: Ctx, p: Principal, ws: dict, f, version: int | None, log_it: bool = True):
    if version is None:
        if f["deleted_at"] is not None:
            raise ApiError(404, "FILE_DELETED", f"'/{f['path']}' was deleted; read an older version or restore it",
                           last_version=f["current_version"], file_id=f["id"])
        version = f["current_version"]
    v = version_row(ctx.db, f["id"], version)
    if not v:
        raise ApiError(404, "VERSION_NOT_FOUND", f"version {version} of '/{f['path']}' not found")
    if v["blob_sha256"] is None:
        raise ApiError(404, "FILE_DELETED", f"version {version} of '/{f['path']}' is a deletion")
    if log_it:
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "read", f["path"], f["id"], buffered=True, version=version)
    return f, v


def read_file(ctx: Ctx, p: Principal, ws: dict, raw_path: str, version: int | None = None):
    f, v = read_version(ctx, p, ws, raw_path, version)
    return f, v, ctx.blobs.open(ws["owner_id"], v["blob_sha256"])


def list_files(ctx: Ctx, p: Principal, ws: dict, prefix: str = "", recursive: bool = False,
               q: str | None = None, include_deleted: bool = False, *, deleted_only: bool = False,
               ext: str | None = None, created_by: str | None = None, modified_after: float | None = None,
               modified_before: float | None = None, after: str | None = None, limit: int | None = None) -> list[dict]:
    """List or search a folder. Filters (spec §18): name substring, extension, creator,
    modification window. Results are always filtered by the caller's permissions.
    `after`/`limit` give keyset pagination over paths."""
    prefix = normalize(prefix, folder=True, allow_root=True)
    allowed = readable_prefixes(ctx.db, p, ws)
    denied = deny_read_prefixes(ctx.db, p, ws)
    searching = bool(q or ext or created_by or modified_after or modified_before or deleted_only)
    rows = ctx.db.all(
        """SELECT f.*, v.size, v.content_type, v.actor, v.blob_sha256 FROM files f
           LEFT JOIN versions v ON v.file_id = f.id AND v.n = f.current_version
           WHERE f.workspace_id = ? AND f.path LIKE ? ESCAPE '\\'
           ORDER BY f.path, (f.deleted_at IS NULL) DESC, f.deleted_at DESC""",
        ws["id"], _like(prefix),
    )
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for r in rows:
        if r["path"] in seen or r["path"] == prefix:
            continue  # older deleted incarnations of a path (live/latest row wins); the folder itself
        seen.add(r["path"])
        is_deleted = r["deleted_at"] is not None
        if deleted_only and not is_deleted:
            continue
        if is_deleted and not (include_deleted or deleted_only):
            continue
        if allowed is not None:
            ok = any(under(r["path"], a) for a in allowed)
            if not ok and r["kind"] == "folder" and any(a.startswith(r["path"]) for a in allowed):
                ok = True  # a folder on the way to something readable
            if not ok:
                continue
        if any(under(r["path"], d) for d in denied):
            continue
        if q and q.lower() not in r["path"].lower():
            continue
        if ext and (r["kind"] != "file" or not r["path"].lower().endswith("." + ext.lower().lstrip("."))):
            continue
        if created_by and r["created_by"] != created_by and r["actor"] != created_by:
            continue
        if modified_after and r["updated_at"] < modified_after:
            continue
        if modified_before and r["updated_at"] > modified_before:
            continue
        rel = r["path"][len(prefix):]
        if not recursive and not searching and "/" in rel.rstrip("/"):
            child = prefix + rel.split("/", 1)[0] + "/"
            out.setdefault(child, {"path": child, "kind": "folder", "implicit": True})
            continue
        out[r["path"]] = {
            "id": r["id"], "path": r["path"], "kind": r["kind"], "version": r["current_version"],
            "deleted": is_deleted, "size": r["size"] or 0,
            "content_type": r["content_type"], "sha256": r["blob_sha256"],
            "updated_at": r["updated_at"], "updated_by": r["actor"], "created_by": r["created_by"],
        }
    entries = sorted(out.values(), key=lambda e: e["path"])
    if after:
        entries = [e for e in entries if e["path"] > after]
    if limit:
        entries = entries[:limit]
    return sorted(entries, key=lambda e: (e["kind"] != "folder", e["path"])) if not limit else entries


def list_versions(ctx: Ctx, p: Principal, ws: dict, raw_path: str) -> list[dict]:
    path = normalize(raw_path)
    require(rights_for(ctx.db, p, ws, path), "read", path)
    f = file_row(ctx.db, ws["id"], path)
    if not f:
        raise file_not_found(f"'/{path}' not found")
    return versions_of(ctx.db, f["id"])


def versions_of(db: Database, file_id: str) -> list[dict]:
    return [dict(v) for v in db.all("SELECT * FROM versions WHERE file_id = ? ORDER BY n DESC", file_id)]


# ---------------------------------------------------------------- writing

def _check_no_conflicting_parents(db: Database, ws_id: str, path: str):
    parts = path.rstrip("/").split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if live_row(db, ws_id, parent) and live_row(db, ws_id, parent)["kind"] == "file":
            raise ApiError(409, "PATH_CONFLICT", f"'/{parent}' is a file, so '/{path}' can't be created under it")
    if not path.endswith("/") and live_row(db, ws_id, path + "/"):
        raise ApiError(409, "PATH_CONFLICT", f"'/{path}' is a folder")


def _ensure_parent_folders(db: Database, ws: dict, path: str, actor: str):
    """Give every folder on the way to `path` a real row (and so a stable id)."""
    parts = path.rstrip("/").split("/")[:-1]
    t = now()
    for i in range(1, len(parts) + 1):
        fp = "/".join(parts[:i]) + "/"
        if not live_row(db, ws["id"], fp):
            db.exec("""INSERT INTO files(id, workspace_id, path, kind, current_version, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,0,?,?,?)""", new_id("fld"), ws["id"], fp, "folder", actor, t, t)


def _apply_version(db: Database, ws: dict, path: str, *, sha: str | None, size: int, ctype: str | None,
                   op: str, actor: str, message: str, proposal_id: str | None = None, prov: dict | None = None) -> dict:
    """Append a version (caller holds the transaction). Writing to a path whose file was
    deleted revives that file, so its history continues."""
    t = now()
    f = file_row(db, ws["id"], path)
    if f is None:
        fid = new_id("fil")
        db.exec("""INSERT INTO files(id, workspace_id, path, kind, current_version, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,0,?,?,?)""", fid, ws["id"], path, "file", actor, t, t)
        n = 1
    else:
        fid = f["id"]
        n = f["current_version"] + 1
    if op == "write":
        op = "update" if f is not None and f["deleted_at"] is None else "create"
    if sha is not None:
        _ensure_parent_folders(db, ws, path, actor)
    vid = new_id("ver")
    db.exec("""INSERT INTO versions(id, file_id, n, blob_sha256, size, content_type, op, actor, message, proposal_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            vid, fid, n, sha, size, ctype, op, actor, message, proposal_id, t)
    pr = P.record(db, version_id=vid, file_id=fid, n=n, owner_id=ws["owner_id"], sha=sha, prov=prov, actor=actor,
                  inherit=op in ("copy", "restore", "move") or prov is None)
    db.exec("UPDATE files SET current_version=?, updated_at=?, deleted_at=? WHERE id=?",
            n, t, t if sha is None else None, fid)
    if sha is not None:
        db.exec("INSERT OR IGNORE INTO blobs(owner_id, sha256, size, created_at) VALUES (?,?,?,?)", ws["owner_id"], sha, size, t)
    invalidate_usage(db, ws["id"])
    return {"id": fid, "path": path, "version": n, "op": op, "size": size, "sha256": sha, "content_type": ctype,
            "trust": pr["trust"]}


def _needs_approval(policy: str, exists: bool) -> bool:
    return policy == "all" or (policy == "overwrite" and exists)


def _propose(db: Database, ws: dict, p: Principal, path: str, op: str, sha, size, ctype, base: int, message: str,
             prov: dict | None = None, reason: str | None = None) -> dict:
    if not p.is_user:
        C.check_budget(db, p.id, 0, proposal=True)
        C.charge(db, p.id, 0, proposal=True)
    pid = new_id("prop")
    db.exec("""INSERT INTO proposals(id, workspace_id, path, op, blob_sha256, size, content_type, base_version, actor, message,
               provenance, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            pid, ws["id"], path, op, sha, size, ctype, base, p.actor, message, json.dumps(prov) if prov else None, now())
    if sha:
        db.exec("INSERT OR IGNORE INTO blobs(owner_id, sha256, size, created_at) VALUES (?,?,?,?)", ws["owner_id"], sha, size, now())
    invalidate_usage(db, ws["id"])
    log(db, ws["owner_id"], ws["id"], p.actor, "propose_" + op, path, proposal_id=pid, base_version=base,
        **({"reason": reason} if reason else {}), **({"trust": prov["trust"]} if prov else {}))
    out = {"status": "pending_approval", "proposal_id": pid, "path": path, "base_version": base}
    if reason:
        out["reason"] = reason
    return out


def check_write(ctx: Ctx, p: Principal, ws: dict, path: str) -> Rights:
    r = rights_for(ctx.db, p, ws, path)
    require(r, "write", path)
    return r


def commit_staged(ctx: Ctx, p: Principal, ws: dict, path: str, staged: Path, sha: str, size: int,
                  ctype: str | None, if_match: int | None, message: str, upload_id: str | None = None) -> dict:
    """Commit already-staged plaintext as the next version of `path` (or a proposal).
    The blob is encrypted into storage before the metadata transaction; if that
    transaction fails, the orphaned blob is reclaimed by the cleanup worker."""
    r = check_write(ctx, p, ws, path)
    ctype = guess_type(path, ctype)
    existing = live_row(ctx.db, ws["id"], path)
    n_recent = C.runaway(ctx.db, p.actor, existing["id"] if existing else None)
    if n_recent:
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "runaway", path, existing["id"], writes=n_recent,
            window_s=C.RUNAWAY_WINDOW)
        raise ApiError(429, "RUNAWAY_SUSPECTED",
                       f"you've written /{path} {n_recent} times in {C.RUNAWAY_WINDOW // 60} minutes; stopping here. "
                       "If this is intended, batch your changes, or ask a human to raise the limit.", writes=n_recent)
    prov = P.capture(ctx.db, p, ws)
    pol = P.policy(ws)
    untrusted_rule = prov["trust"] == "untrusted" and pol["untrusted"] != "allow" and P.applies(pol, path)
    if untrusted_rule and pol["untrusted"] == "block":
        raise ApiError(403, "UNTRUSTED_CONTENT",
                       f"/{path} is protected: this workspace doesn't accept content derived from untrusted input there",
                       reasons=prov["reasons"])
    ctx.blobs.put_file(ws["owner_id"], staged, sha)
    with ctx.db.tx():
        C.check_write(ctx.db, ws, p.actor, path, P.current().fence)
        f = live_row(ctx.db, ws["id"], path)
        if f is None and live_row(ctx.db, ws["id"], path + "/"):
            raise ApiError(409, "PATH_CONFLICT", f"'/{path}' is a folder")
        _check_no_conflicting_parents(ctx.db, ws["id"], path)
        cur = live_version(f)
        if if_match is not None and if_match != cur:
            raise ApiError(409, "VERSION_CONFLICT",
                           f"'/{path}' is at version {cur}, you sent If-Match {if_match}", current_version=cur)
        if cur and f is not None:
            v = version_row(ctx.db, f["id"], cur)
            if v and v["blob_sha256"] == sha and v["content_type"] == ctype:
                return {"id": f["id"], "path": path, "version": cur, "op": "unchanged", "size": size, "sha256": sha, "content_type": ctype}
        ensure_quota(ctx.db, ws, size, sha, exclude_upload=upload_id)
        if _needs_approval(r.write_approval, cur > 0) or untrusted_rule:
            return _propose(ctx.db, ws, p, path, "write", sha, size, ctype, cur, message, prov,
                            reason="untrusted_content" if untrusted_rule and not _needs_approval(r.write_approval, cur > 0) else None)
        if not p.is_user:
            C.check_budget(ctx.db, p.id, size, proposal=False)
            C.charge(ctx.db, p.id, size)
        res = _apply_version(ctx.db, ws, path, sha=sha, size=size, ctype=ctype, op="write", actor=p.actor, message=message,
                             prov=prov)
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, res["op"], path, res["id"], version=res["version"], size=size,
            trust=res["trust"])
        return res


def write_file(ctx: Ctx, p: Principal, ws: dict, raw_path: str, data: bytes, content_type: str | None = None,
               if_match: int | None = None, message: str = "") -> dict:
    path = normalize(raw_path)
    check_write(ctx, p, ws, path)
    staged, sha, size = ctx.blobs.stage_bytes(data)
    try:
        return commit_staged(ctx, p, ws, path, staged, sha, size, content_type, if_match, message)
    finally:
        staged.unlink(missing_ok=True)


# ---------------------------------------------------------------- agent-native edits (send diffs, not whole files)

def _current_text(ctx: Ctx, p: Principal, ws: dict, path: str) -> tuple[str | None, int]:
    f = live_row(ctx.db, ws["id"], path)
    if not f or f["kind"] != "file":
        return None, 0
    _, v = resolve_version(ctx, p, ws, f, None, log_it=False)
    raw = ctx.blobs.open(ws["owner_id"], v["blob_sha256"])
    try:
        return raw.decode("utf-8"), f["current_version"]
    except UnicodeDecodeError:
        raise ApiError(400, "NOT_TEXT", f"'/{path}' is binary; edit and append work on UTF-8 text")


def edit_file(ctx: Ctx, p: Principal, ws: dict, raw_path: str, edits: list[dict], message: str = "",
              if_match: int | None = None) -> dict:
    """Apply exact-text replacements: each edit's `old` must appear exactly once (unless
    replace_all). All edits apply or none do. Saves output tokens and latency versus
    rewriting a whole file, and it's a compare-and-swap on the version it read, so a
    concurrent change by someone else produces a conflict instead of being lost."""
    path = normalize(raw_path)
    require(rights_for(ctx.db, p, ws, path), "read", path)
    check_write(ctx, p, ws, path)
    if not edits:
        raise ApiError(400, "INVALID_REQUEST", "no edits given")
    text, ver = _current_text(ctx, p, ws, path)
    if text is None:
        raise file_not_found(f"'/{path}' not found; use write to create it")
    if if_match is not None and if_match != ver:
        raise ApiError(409, "VERSION_CONFLICT", f"'/{path}' is at version {ver}, you sent {if_match}", current_version=ver)
    for i, e in enumerate(edits, 1):
        old, new = e.get("old", ""), e.get("new", "")
        if not old:
            raise ApiError(400, "INVALID_EDIT", f"edit {i}: `old` must be non-empty")
        n = text.count(old)
        if n == 0:
            raise ApiError(422, "EDIT_NOT_FOUND", f"edit {i}: text not found in /{path} (v{ver}); re-read it and retry", edit=i)
        if n > 1 and not e.get("replace_all"):
            raise ApiError(422, "EDIT_AMBIGUOUS", f"edit {i}: text appears {n} times; add surrounding context or set replace_all", edit=i, matches=n)
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    res = write_file(ctx, p, ws, path, text.encode("utf-8"), None, ver, message or f"{len(edits)} edit(s)")
    res["edits"] = len(edits)
    return res


def append_file(ctx: Ctx, p: Principal, ws: dict, raw_path: str, content: str, message: str = "") -> dict:
    """Append text (creating the file if needed): for logs, journals and running notes.
    Retries once if someone else wrote in between."""
    path = normalize(raw_path)
    check_write(ctx, p, ws, path)
    for attempt in range(2):
        text, ver = _current_text(ctx, p, ws, path) if live_row(ctx.db, ws["id"], path) else (None, 0)
        try:
            return write_file(ctx, p, ws, path, ((text or "") + content).encode("utf-8"), None, ver, message or "append")
        except ApiError as e:
            if e.code != "VERSION_CONFLICT" or attempt:
                raise


def slice_lines(text: str, offset: int | None, limit: int | None, numbered: bool) -> tuple[str, int, int, int]:
    """Return (text, first_line, last_line, total_lines) for a 1-based line window."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(1, offset or 1)
    end = total if not limit else min(total, start + limit - 1)
    window = lines[start - 1:end]
    if numbered:
        width = len(str(end or 1))
        window = [f"{str(start + i).rjust(width)}\t{ln}" for i, ln in enumerate(window)]
    return "".join(window), start, end, total


def delete_file(ctx: Ctx, p: Principal, ws: dict, raw_path: str, if_match: int | None = None, message: str = "") -> dict:
    raw = raw_path.strip()
    if raw.endswith("/"):
        return delete_folder(ctx, p, ws, raw, message)
    path = normalize(raw)
    if not live_row(ctx.db, ws["id"], path) and live_row(ctx.db, ws["id"], path + "/"):
        return delete_folder(ctx, p, ws, path + "/", message)  # they meant the folder
    with ctx.db.tx():
        f = live_row(ctx.db, ws["id"], path)
        if f is None:
            raise file_not_found(f"'/{path}' not found")
        r = rights_for(ctx.db, p, ws, path)
        require(r, "delete", path)
        C.check_write(ctx.db, ws, p.actor, path, P.current().fence)
        cur = f["current_version"]
        if if_match is not None and if_match != cur:
            raise ApiError(409, "VERSION_CONFLICT", f"'/{path}' is at version {cur}", current_version=cur)
        if _needs_approval(r.delete_approval, True):
            return _propose(ctx.db, ws, p, path, "delete", None, 0, None, cur, message)
        res = _apply_version(ctx.db, ws, path, sha=None, size=0, ctype=None, op="delete", actor=p.actor, message=message)
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "delete", path, res["id"], version=res["version"])
        return res


def create_folder(ctx: Ctx, p: Principal, ws: dict, raw_path: str) -> dict:
    path = normalize(raw_path, folder=True)
    require(rights_for(ctx.db, p, ws, path), "write", path)
    with ctx.db.tx():
        _check_no_conflicting_parents(ctx.db, ws["id"], path)
        if live_row(ctx.db, ws["id"], path.rstrip("/")):
            raise ApiError(409, "PATH_CONFLICT", f"'/{path.rstrip('/')}' is a file")
        existing = live_row(ctx.db, ws["id"], path)
        if existing:
            return {"id": existing["id"], "path": path, "kind": "folder"}
        _ensure_parent_folders(ctx.db, ws, path, p.actor)
        t = now()
        fid = new_id("fld")
        ctx.db.exec("""INSERT INTO files(id, workspace_id, path, kind, current_version, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,0,?,?,?)""", fid, ws["id"], path, "folder", p.actor, t, t)
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "mkdir", path, fid)
    return {"id": fid, "path": path, "kind": "folder"}


def delete_folder(ctx: Ctx, p: Principal, ws: dict, raw_path: str, message: str = "") -> dict:
    path = normalize(raw_path, folder=True)
    require(rights_for(ctx.db, p, ws, path), "delete", path)
    with ctx.db.tx():
        live = ctx.db.one("""SELECT COUNT(*) AS c FROM files WHERE workspace_id=? AND path LIKE ? ESCAPE '\\'
                             AND path != ? AND deleted_at IS NULL""", ws["id"], _like(path), path)
        if live["c"]:
            raise ApiError(409, "FOLDER_NOT_EMPTY", f"'/{path}' contains {live['c']} item(s); delete them first")
        f = live_row(ctx.db, ws["id"], path)
        if not f:
            raise not_found(f"'/{path}' not found", "FOLDER_NOT_FOUND")
        ctx.db.exec("UPDATE files SET deleted_at=?, updated_at=? WHERE id=?", now(), now(), f["id"])
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "rmdir", path, f["id"])
    return {"id": f["id"], "path": path, "op": "delete"}


def _repoint(db: Database, ws_id: str, old: str, new: str):
    """Things addressed by path follow a move: grants, share links, pending proposals."""
    if old.endswith("/"):
        n = len(old)
        db.exec("UPDATE grants SET path_prefix = ? || substr(path_prefix, ?) WHERE workspace_id=? AND path_prefix LIKE ? ESCAPE '\\' AND revoked_at IS NULL",
                new, n + 1, ws_id, _like(old))
        db.exec("UPDATE share_links SET path = ? || substr(path, ?) WHERE workspace_id=? AND path LIKE ? ESCAPE '\\'", new, n + 1, ws_id, _like(old))
        db.exec("UPDATE proposals SET path = ? || substr(path, ?) WHERE workspace_id=? AND status='pending' AND path LIKE ? ESCAPE '\\'",
                new, n + 1, ws_id, _like(old))
    else:
        db.exec("UPDATE grants SET path_prefix=? WHERE workspace_id=? AND path_prefix=? AND revoked_at IS NULL", new, ws_id, old)
        db.exec("UPDATE share_links SET path=? WHERE workspace_id=? AND path=?", new, ws_id, old)
        db.exec("UPDATE proposals SET path=? WHERE workspace_id=? AND status='pending' AND path=?", new, ws_id, old)


def move(ctx: Ctx, p: Principal, ws: dict, src_raw: str, dst_raw: str, message: str = "") -> dict:
    """Rename or move a file or folder. The file keeps its id and history (Decision 2):
    only its path changes. Folder moves carry everything inside. Atomic."""
    src_s = src_raw.strip()
    is_folder = src_s.endswith("/") or (not live_row(ctx.db, ws["id"], normalize(src_s))
                                        and bool(live_row(ctx.db, ws["id"], normalize(src_s, folder=True))))
    with ctx.db.tx():
        if is_folder:
            src, dst = normalize(src_s, folder=True), normalize(dst_raw, folder=True)
            if dst.startswith(src):
                raise ApiError(400, "INVALID_MOVE", "can't move a folder into itself")
            rows = ctx.db.all("SELECT * FROM files WHERE workspace_id=? AND path LIKE ? ESCAPE '\\' AND deleted_at IS NULL ORDER BY path",
                              ws["id"], _like(src))
            if not rows:
                raise not_found(f"'/{src}' not found", "FOLDER_NOT_FOUND")
        else:
            src, dst = normalize(src_s), normalize(dst_raw)
            f = live_row(ctx.db, ws["id"], src)
            if not f or f["kind"] != "file":
                raise file_not_found(f"'/{src}' not found")
            rows = [f]
        if src == dst:
            return {"moved": []}
        pairs = [(r, dst + r["path"][len(src):]) if is_folder else (r, dst) for r in rows]
        for f, new_path in pairs:
            rs, rd = rights_for(ctx.db, p, ws, f["path"]), rights_for(ctx.db, p, ws, new_path)
            require(rs, "read", f["path"]); require(rs, "delete", f["path"]); require(rd, "write", new_path)
            C.check_write(ctx.db, ws, p.actor, f["path"], P.current().fence)
            C.check_write(ctx.db, ws, p.actor, new_path, None)
            if _needs_approval(rs.delete_approval, True) or _needs_approval(rd.write_approval, False):
                raise ApiError(409, "APPROVAL_REQUIRED",
                               "this move needs human approval under your grant; write the new file and delete the old one instead")
            if live_row(ctx.db, ws["id"], new_path):
                raise ApiError(409, "ALREADY_EXISTS", f"'/{new_path}' already exists")
        _check_no_conflicting_parents(ctx.db, ws["id"], dst)
        t = now()
        results = []
        for f, new_path in pairs:
            ctx.db.exec("UPDATE files SET path=?, updated_at=? WHERE id=?", new_path, t, f["id"])
            results.append({"id": f["id"], "from": f["path"], "to": new_path, "version": f["current_version"]})
        _ensure_parent_folders(ctx.db, ws, dst, p.actor)
        _repoint(ctx.db, ws["id"], src, dst)
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "move", src, rows[0]["id"] if not is_folder else None,
            to=dst, count=len(results), message=message)
    return {"moved": results}


def copy(ctx: Ctx, p: Principal, ws: dict, src_raw: str, dst_raw: str, message: str = "") -> dict:
    """Duplicate a file's current content as a new, independent file."""
    src, dst = normalize(src_raw), normalize(dst_raw)
    f, v = read_version(ctx, p, ws, src, log_it=False)
    r = check_write(ctx, p, ws, dst)
    with ctx.db.tx():
        if live_row(ctx.db, ws["id"], dst):
            raise ApiError(409, "ALREADY_EXISTS", f"'/{dst}' already exists")
        _check_no_conflicting_parents(ctx.db, ws["id"], dst)
        if _needs_approval(r.write_approval, False):
            return _propose(ctx.db, ws, p, dst, "write", v["blob_sha256"], v["size"], v["content_type"], 0, message or f"copy of /{src}")
        res = _apply_version(ctx.db, ws, dst, sha=v["blob_sha256"], size=v["size"], ctype=v["content_type"], op="copy",
                             actor=p.actor, message=message or f"copied from /{src} v{v['n']}")
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "copy", dst, res["id"], source=src, source_id=f["id"], version=res["version"])
        return res


def restore(ctx: Ctx, p: Principal, ws: dict, raw_path: str, version: int, message: str = "", file_id: str | None = None) -> dict:
    """Make an old version current again by appending it as a new version. History is
    never rewritten, so a restore can itself be undone. Works on deleted files too."""
    with ctx.db.tx():
        if file_id:
            f = ctx.db.one("SELECT * FROM files WHERE id=? AND workspace_id=?", file_id, ws["id"])
        else:
            f = file_row(ctx.db, ws["id"], normalize(raw_path))
        if not f or f["kind"] != "file":
            raise file_not_found(f"'/{raw_path}' not found")
        path = f["path"]
        r = rights_for(ctx.db, p, ws, path)
        require(r, "write", path)
        C.check_write(ctx.db, ws, p.actor, path, P.current().fence)
        v = version_row(ctx.db, f["id"], version)
        if not v:
            raise ApiError(404, "VERSION_NOT_FOUND", f"version {version} of '/{path}' not found")
        if v["blob_sha256"] is None:
            raise ApiError(400, "INVALID_RESTORE", f"version {version} is a deletion; restore an earlier version")
        if f["current_version"] == version and f["deleted_at"] is None:
            return {"id": f["id"], "path": path, "version": version, "op": "unchanged"}
        if f["deleted_at"] is not None:
            other = live_row(ctx.db, ws["id"], path)
            if other:
                raise ApiError(409, "PATH_CONFLICT", f"another file now lives at '/{path}'; move it first")
        cur = live_version(f)
        if _needs_approval(r.write_approval, cur > 0):
            return _propose(ctx.db, ws, p, path, "write", v["blob_sha256"], v["size"], v["content_type"], cur,
                            message or f"restore version {version}")
        ensure_quota(ctx.db, ws, v["size"], v["blob_sha256"])
        n = f["current_version"] + 1
        t = now()
        vid = new_id("ver")
        ctx.db.exec("""INSERT INTO versions(id, file_id, n, blob_sha256, size, content_type, op, actor, message, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", vid, f["id"], n, v["blob_sha256"], v["size"], v["content_type"],
                    "restore", p.actor, message or f"restored version {version}", t)
        old = ctx.db.one("SELECT trust, reasons FROM provenance WHERE version_id=?", v["id"])
        P.record(ctx.db, version_id=vid, file_id=f["id"], n=n, owner_id=ws["owner_id"], sha=v["blob_sha256"],
                 prov={"actor": p.actor, "credential_id": p.token_id, "inputs": [(f["id"], version)], "sources": [],
                       "trust": old["trust"] if old and old["trust"] != "endorsed" else "trusted",
                       "reasons": json.loads(old["reasons"]) if old else []}, actor=p.actor)
        ctx.db.exec("UPDATE files SET current_version=?, updated_at=?, deleted_at=NULL WHERE id=?", n, t, f["id"])
        _ensure_parent_folders(ctx.db, ws, path, p.actor)
        invalidate_usage(ctx.db, ws["id"])
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "restore", path, f["id"], from_version=version, version=n)
        return {"id": f["id"], "path": path, "version": n, "op": "restore", "size": v["size"], "sha256": v["blob_sha256"]}


# ---------------------------------------------------------------- approvals

def decide_proposal(ctx: Ctx, p: Principal, proposal_id: str, approve: bool, note: str = "", force: bool = False) -> dict:
    if not p.is_user:
        raise forbidden("only a human user can approve or reject changes")
    with ctx.db.tx():
        pr = ctx.db.one("SELECT * FROM proposals WHERE id=?", proposal_id)
        if not pr:
            raise not_found("proposal not found", "APPROVAL_NOT_FOUND")
        ws = get_workspace(ctx, p, pr["workspace_id"])
        if pr["status"] != "pending":
            raise ApiError(409, "ALREADY_DECIDED", f"proposal is {pr['status']}")
        t = now()
        if not approve:
            ctx.db.exec("UPDATE proposals SET status='rejected', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                        p.actor, t, note, proposal_id)
            log(ctx.db, ws["owner_id"], ws["id"], p.actor, "reject", pr["path"], proposal_id=proposal_id, by=pr["actor"])
            return {"status": "rejected", "proposal_id": proposal_id}
        f = live_row(ctx.db, ws["id"], pr["path"])
        cur = live_version(f)
        if cur != pr["base_version"] and not force:
            raise ApiError(409, "STALE_PROPOSAL",
                           f"'/{pr['path']}' changed since this was proposed (base v{pr['base_version']}, now v{cur}). "
                           "Review the diff again and approve with force=true, or reject.", current_version=cur)
        if pr["op"] == "delete":
            if cur == 0:
                res = {"path": pr["path"], "op": "unchanged"}
            else:
                res = _apply_version(ctx.db, ws, pr["path"], sha=None, size=0, ctype=None, op="delete",
                                     actor=pr["actor"], message=pr["message"], proposal_id=proposal_id)
        else:
            _check_no_conflicting_parents(ctx.db, ws["id"], pr["path"])
            prov = json.loads(pr["provenance"]) if pr["provenance"] else None
            if prov:
                prov["inputs"] = [tuple(i) for i in prov.get("inputs") or []]
            res = _apply_version(ctx.db, ws, pr["path"], sha=pr["blob_sha256"], size=pr["size"], ctype=pr["content_type"],
                                 op="write", actor=pr["actor"], message=pr["message"], proposal_id=proposal_id, prov=prov)
        ctx.db.exec("UPDATE proposals SET status='approved', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                    p.actor, t, note, proposal_id)
        log(ctx.db, ws["owner_id"], ws["id"], p.actor, "approve", pr["path"], res.get("id"), proposal_id=proposal_id,
            by=pr["actor"], version=res.get("version"))
        return {"status": "approved", "proposal_id": proposal_id, "applied": res}


# ---------------------------------------------------------------- diffs

def _text(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _csv_summary(a: str, b: str) -> dict | None:
    try:
        ra = list(csv.reader(io.StringIO(a)))
        rb = list(csv.reader(io.StringIO(b)))
    except csv.Error:
        return None
    if not ra or not rb:
        return None
    ha, hb = ra[0], rb[0]
    return {"columns_removed": [c for c in ha if c not in hb], "columns_added": [c for c in hb if c not in ha],
            "rows_before": len(ra) - 1, "rows_after": len(rb) - 1}


DIFF_MAX = 8 * 1024 * 1024


def diff_bytes(path: str, a: bytes | None, b: bytes | None, label_a: str, label_b: str, max_lines: int = 2000) -> dict:
    a = a or b""
    b = b or b""
    out: dict = {"path": path, "from": label_a, "to": label_b, "size_from": len(a), "size_to": len(b), "identical": a == b}
    ta, tb = _text(a), _text(b)
    if ta is None or tb is None:
        out["kind"] = "binary"
        return out
    out["kind"] = "text"
    lines = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), label_a, label_b, lineterm=""))
    out["truncated"] = len(lines) > max_lines
    out["unified"] = "\n".join(lines[:max_lines])
    out["added"] = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    out["removed"] = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if path.lower().endswith((".csv", ".tsv")):
        out["csv"] = _csv_summary(ta, tb)
    return out


def blob_or_none(ctx: Ctx, ns: str, sha, size: int = 0):
    if not sha:
        return None
    if size > DIFF_MAX:
        return b"\x00"  # too big to diff: report as binary
    return ctx.blobs.open(ns, sha)


def diff_file(ctx: Ctx, ws: dict, f, a: int, b: int | None) -> dict:
    b = b or f["current_version"]
    va, vb = version_row(ctx.db, f["id"], a), version_row(ctx.db, f["id"], b)
    if not va or not vb:
        raise ApiError(404, "VERSION_NOT_FOUND", "version not found")
    ns = ws["owner_id"]
    return diff_bytes(f["path"], blob_or_none(ctx, ns, va["blob_sha256"], va["size"]),
                      blob_or_none(ctx, ns, vb["blob_sha256"], vb["size"]), f"v{a}", f"v{b}")


def diff_versions(ctx: Ctx, p: Principal, ws: dict, raw_path: str, a: int, b: int | None) -> dict:
    path = normalize(raw_path)
    require(rights_for(ctx.db, p, ws, path), "read", path)
    f = file_row(ctx.db, ws["id"], path)
    if not f:
        raise file_not_found(f"'/{path}' not found")
    return diff_file(ctx, ws, f, a, b)


def proposal_detail(ctx: Ctx, p: Principal, proposal_id: str) -> dict:
    pr = ctx.db.one("SELECT * FROM proposals WHERE id=?", proposal_id)
    if not pr:
        raise not_found("proposal not found", "APPROVAL_NOT_FOUND")
    ws = get_workspace(ctx, p, pr["workspace_id"])
    if not p.is_user and pr["actor"] != p.actor:
        raise not_found("proposal not found", "APPROVAL_NOT_FOUND")
    d = dict(pr)
    f = live_row(ctx.db, ws["id"], pr["path"])
    cur = live_version(f)
    d["current_version"] = cur
    d["stale"] = cur != pr["base_version"]
    base_v = version_row(ctx.db, f["id"], cur) if f and cur else None
    ns = ws["owner_id"]
    d["diff"] = diff_bytes(pr["path"], blob_or_none(ctx, ns, base_v["blob_sha256"], base_v["size"]) if base_v else None,
                           blob_or_none(ctx, ns, pr["blob_sha256"], pr["size"]), f"current (v{cur})", "proposed")
    return d
