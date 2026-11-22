"""Data plane: upload sessions and signed transfer URLs (spec §5.3–5.5, §16).

Control plane decides (permissions, quota reservation, state); data plane moves bytes
through short-lived URLs signed for exactly one object and one operation. Against a
cloud object store these URLs would be S3/R2 presigned URLs; here they point at this
server's own /v1/data endpoints, which stream to/from the encrypted blob store, so the
state machine and API are the same either way.

Upload state machine:  CREATED -> UPLOADED -> COMMITTED
                       (FAILED | EXPIRED | ABORTED from any non-terminal state)
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Iterator

from . import service as S
from .auth import Principal
from .db import new_id, now
from .paths import normalize
from .service import ApiError, Ctx
from .storage import TooLarge

UPLOAD_TTL = 15 * 60
DOWNLOAD_TTL = 5 * 60
_key = b""


def set_signing_key(master_key: bytes) -> None:
    global _key
    _key = hmac.new(master_key, b"agentcloud/signed-url/v1", hashlib.sha256).digest()


def _sig(*parts) -> str:
    return hmac.new(_key, "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def _check_sig(sig: str, exp: int, *parts):
    if exp < now():
        raise ApiError(403, "URL_EXPIRED", "this signed URL has expired; request a new one")
    if not hmac.compare_digest(sig or "", _sig(*parts, exp)):
        raise ApiError(403, "INVALID_SIGNATURE", "signed URL is invalid")


def _upload_out(u, with_url: bool = False) -> dict:
    d = {"upload_id": u["id"], "workspace_id": u["workspace_id"], "path": u["path"], "state": u["state"],
         "size_bytes": u["declared_size"], "content_type": u["content_type"], "expires_at": u["expires_at"],
         "received_bytes": u["received_size"], "checksum_sha256": u["received_sha256"]}
    if u["result"]:
        d["result"] = json.loads(u["result"])
    if u["error"]:
        d["error"] = json.loads(u["error"])
    if with_url:
        exp = int(u["expires_at"])
        d["upload_url"] = f"/v1/data/uploads/{u['id']}?exp={exp}&sig={_sig('up', u['id'], exp)}"
        d["upload_method"] = "PUT"
    return d


def create_upload(ctx: Ctx, p: Principal, ws: dict, raw_path: str, size: int, content_type: str | None,
                  checksum: str | None, if_version: int | None, message: str, max_bytes: int) -> dict:
    path = normalize(raw_path)
    S.check_write(ctx, p, ws, path)
    if size < 0:
        raise ApiError(400, "INVALID_REQUEST", "size_bytes must be >= 0")
    if size > max_bytes:
        raise ApiError(413, "FILE_TOO_LARGE", f"max file size is {max_bytes} bytes")
    if checksum and (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum.lower())):
        raise ApiError(400, "INVALID_REQUEST", "checksum_sha256 must be 64 hex characters")
    t = now()
    uid = new_id("upl")
    with ctx.db.tx():  # quota check + reservation are one atomic step (§13.4)
        S.ensure_quota(ctx.db, ws, size)
        ctx.db.exec("""INSERT INTO uploads(id, workspace_id, owner_id, actor, path, content_type, declared_size, declared_sha256,
                       if_version, message, state, created_at, expires_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    uid, ws["id"], ws["owner_id"], p.actor, path, content_type, size, checksum.lower() if checksum else None,
                    if_version, message, "CREATED", t, t + UPLOAD_TTL, t)
    S.invalidate_usage(ctx.db, ws["id"])
    S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, "upload_started", path, upload_id=uid, size=size)
    return _upload_out(ctx.db.one("SELECT * FROM uploads WHERE id=?", uid), with_url=True)


def check_upload_url(ctx: Ctx, upload_id: str, exp: int, sig: str) -> int:
    """Validate a signed upload URL before accepting any bytes. Returns the declared size."""
    _check_sig(sig, exp, "up", upload_id)
    u = ctx.db.one("SELECT * FROM uploads WHERE id=?", upload_id)
    if not u or u["state"] not in ("CREATED", "UPLOADED") or u["expires_at"] < now():
        raise ApiError(410, "UPLOAD_EXPIRED", "this upload is no longer accepting data")
    return u["declared_size"]


def receive(ctx: Ctx, upload_id: str, exp: int, sig: str, chunks: Iterator[bytes]) -> dict:
    """The signed PUT from a sync caller: stage the stream, then record it."""
    declared = check_upload_url(ctx, upload_id, exp, sig)
    try:
        staged, sha, size = ctx.blobs.stage_stream(chunks, declared)
    except TooLarge:
        raise ApiError(413, "UPLOAD_SIZE_MISMATCH", f"more than the declared {declared} bytes were sent")
    return record_received(ctx, upload_id, staged, sha, size)


def record_received(ctx: Ctx, upload_id: str, staged: Path, sha: str, size: int) -> dict:
    """Bytes are on disk in staging: check the size and move the session to UPLOADED."""
    u = ctx.db.one("SELECT * FROM uploads WHERE id=?", upload_id)
    if not u or u["state"] not in ("CREATED", "UPLOADED") or u["expires_at"] < now():
        staged.unlink(missing_ok=True)
        raise ApiError(410, "UPLOAD_EXPIRED", "this upload is no longer accepting data")
    if size != u["declared_size"]:
        staged.unlink(missing_ok=True)
        raise ApiError(400, "UPLOAD_SIZE_MISMATCH", f"declared {u['declared_size']} bytes, received {size}")
    with ctx.db.tx():
        old = ctx.db.one("SELECT staged_path FROM uploads WHERE id=?", upload_id)["staged_path"]
        ctx.db.exec("""UPDATE uploads SET state='UPLOADED', staged_path=?, received_size=?, received_sha256=?, updated_at=?
                       WHERE id=? AND state IN ('CREATED','UPLOADED')""", str(staged), size, sha, now(), upload_id)
    if old and old != str(staged):
        Path(old).unlink(missing_ok=True)  # a re-PUT replaces the earlier staged bytes
    return {"upload_id": upload_id, "state": "UPLOADED", "received_bytes": size, "checksum_sha256": sha}


def _owned_upload(ctx: Ctx, p: Principal, upload_id: str):
    u = ctx.db.one("SELECT * FROM uploads WHERE id=?", upload_id)
    if not u or u["owner_id"] != p.owner_id or (not p.is_user and u["actor"] != p.actor):
        raise ApiError(404, "UPLOAD_NOT_FOUND", "upload not found")
    return u


def complete(ctx: Ctx, p: Principal, upload_id: str, checksum: str | None) -> dict:
    """Verify what arrived, then commit it as a version (or a proposal). Idempotent:
    completing a committed upload returns the original result."""
    u = _owned_upload(ctx, p, upload_id)
    if u["state"] == "COMMITTED":
        return _upload_out(u)
    if u["state"] == "CREATED":
        raise ApiError(409, "UPLOAD_INCOMPLETE", "no data has been uploaded yet; PUT the file to upload_url first")
    if u["state"] != "UPLOADED":
        raise ApiError(410, "UPLOAD_EXPIRED", f"upload is {u['state']}")
    expected = (checksum or u["declared_sha256"] or "").lower()
    if not expected:
        raise ApiError(400, "CHECKSUM_REQUIRED", "send checksum_sha256 so the upload can be verified")
    if u["declared_sha256"] and checksum and checksum.lower() != u["declared_sha256"]:
        raise ApiError(400, "CHECKSUM_MISMATCH", "checksum differs from the one declared when the upload started")
    staged = Path(u["staged_path"])
    if expected != u["received_sha256"] or not staged.exists():
        _fail(ctx, u, ApiError(422, "CHECKSUM_MISMATCH", "uploaded bytes don't match checksum_sha256; start a new upload"))
    ws = S.get_workspace(ctx, p, u["workspace_id"])
    try:
        res = S.commit_staged(ctx, p, ws, u["path"], staged, u["received_sha256"], u["received_size"],
                              u["content_type"], u["if_version"], u["message"], upload_id=upload_id)
    except ApiError as e:
        _fail(ctx, u, e)
    ctx.db.exec("UPDATE uploads SET state='COMMITTED', result=?, staged_path=NULL, updated_at=? WHERE id=?",
                json.dumps(res), now(), upload_id)
    S.invalidate_usage(ctx.db, u["workspace_id"])
    staged.unlink(missing_ok=True)
    return _upload_out(ctx.db.one("SELECT * FROM uploads WHERE id=?", upload_id))


def _fail(ctx: Ctx, u, e: ApiError):
    S.invalidate_usage(ctx.db, u["workspace_id"])
    ctx.db.exec("UPDATE uploads SET state='FAILED', error=?, staged_path=NULL, updated_at=? WHERE id=?",
                json.dumps({"code": e.code, "message": e.message}), now(), u["id"])
    if u["staged_path"]:
        Path(u["staged_path"]).unlink(missing_ok=True)
    raise e


def abort(ctx: Ctx, p: Principal, upload_id: str) -> dict:
    u = _owned_upload(ctx, p, upload_id)
    if u["state"] in ("CREATED", "UPLOADED"):
        ctx.db.exec("UPDATE uploads SET state='ABORTED', staged_path=NULL, updated_at=? WHERE id=?", now(), upload_id)
        S.invalidate_usage(ctx.db, u["workspace_id"])
        if u["staged_path"]:
            Path(u["staged_path"]).unlink(missing_ok=True)
    return _upload_out(ctx.db.one("SELECT * FROM uploads WHERE id=?", upload_id))


def get(ctx: Ctx, p: Principal, upload_id: str) -> dict:
    return _upload_out(_owned_upload(ctx, p, upload_id))


# ---------------------------------------------------------------- downloads

def download_url(ctx: Ctx, p: Principal, ws: dict, f, version: int | None) -> dict:
    """Authorise now, transfer later: a URL valid for one version of one file, briefly."""
    f, v = S.resolve_version(ctx, p, ws, f, version, log_it=False)
    exp = int(now() + DOWNLOAD_TTL)
    S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, "download", f["path"], f["id"], buffered=True, version=v["n"])
    return {"file_id": f["id"], "version": v["n"], "size": v["size"], "content_type": v["content_type"],
            "checksum_sha256": v["blob_sha256"], "expires_at": exp,
            "url": f"/v1/data/files/{f['id']}/{v['n']}?exp={exp}&sig={_sig('dl', f['id'], v['n'], exp)}"}


def open_download(ctx: Ctx, file_id: str, n: int, exp: int, sig: str):
    _check_sig(sig, exp, "dl", file_id, n)
    row = ctx.db.one("""SELECT f.path, w.owner_id, v.blob_sha256, v.size, v.content_type FROM files f
                        JOIN workspaces w ON w.id=f.workspace_id JOIN versions v ON v.file_id=f.id AND v.n=?
                        WHERE f.id=? AND w.deleted_at IS NULL""", n, file_id)
    if not row or not row["blob_sha256"]:
        raise ApiError(404, "FILE_NOT_FOUND", "file not found")
    return row, ctx.blobs.open_stream(row["owner_id"], row["blob_sha256"])
