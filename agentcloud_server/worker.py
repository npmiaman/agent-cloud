"""Background worker (spec §13, §16.2, §19).

    agentcloud worker --data ./data            # loop forever
    agentcloud worker --data ./data --once     # one pass, print stats

Each pass:
  1. Expires upload sessions past their deadline and frees their reserved quota.
  2. Purges deleted files older than their workspace's retention_days (if set).
  3. Garbage-collects blobs nothing references any more (orphans from failed commits,
     rejected proposals, purged files), after a grace period so in-flight commits are safe.
  4. Removes stale staging files and expired idempotency records.
Every step is idempotent, so a crash mid-pass is fixed by the next pass.
"""
from __future__ import annotations

import time
from pathlib import Path

from .db import now
from .service import Ctx, flush_activity, invalidate_usage, log

BLOB_GRACE = 3600          # never GC a blob younger than this
STAGING_MAX_AGE = 24 * 3600
IDEMPOTENCY_TTL = 24 * 3600


def run_once(ctx: Ctx, blob_grace: float = BLOB_GRACE, data_dir=None) -> dict:
    db, t = ctx.db, now()
    stats = {"uploads_expired": 0, "files_purged": 0, "blobs_deleted": 0, "staging_removed": 0, "idempotency_removed": 0,
             "chunks_deleted": 0}

    flush_activity(db)
    # 1. expire uploads
    for u in db.all("SELECT * FROM uploads WHERE state IN ('CREATED','UPLOADED') AND expires_at < ?", t):
        db.exec("UPDATE uploads SET state='EXPIRED', staged_path=NULL, updated_at=? WHERE id=? AND state IN ('CREATED','UPLOADED')", t, u["id"])
        if u["staged_path"]:
            Path(u["staged_path"]).unlink(missing_ok=True)
        invalidate_usage(db, u["workspace_id"])
        stats["uploads_expired"] += 1

    # 2. retention: purge long-deleted files (hard delete of their versions)
    for ws in db.all("SELECT * FROM workspaces WHERE retention_days IS NOT NULL"):
        cutoff = t - ws["retention_days"] * 86400
        for f in db.all("SELECT id, path FROM files WHERE workspace_id=? AND deleted_at IS NOT NULL AND deleted_at < ?", ws["id"], cutoff):
            with db.tx():
                db.exec("DELETE FROM versions WHERE file_id=?", f["id"])
                db.exec("DELETE FROM files WHERE id=?", f["id"])
                log(db, ws["owner_id"], ws["id"], "system:worker", "purge", f["path"], f["id"], retention_days=ws["retention_days"])
            invalidate_usage(db, ws["id"])
            stats["files_purged"] += 1

    # 2b. keep the search index current (searches also drain it for their own workspaces)
    from . import search_index
    stats["search_indexed"] = search_index.drain(ctx, None, limit=5000)
    from . import vector_index
    stats["vectors_embedded"] = vector_index.drain(ctx, None, limit=2000)["embedded"]

    # 2c. background dedup: rewrite big whole-file blobs as shared content-defined chunks
    stats.update(dedup(ctx))

    # 3. blob GC per organisation namespace
    for org in db.all("SELECT id FROM organisations"):
        ns = org["id"]
        live = {r[0] for r in db.all("""SELECT v.blob_sha256 FROM versions v JOIN files f ON f.id=v.file_id
                                        JOIN workspaces w ON w.id=f.workspace_id WHERE w.owner_id=? AND v.blob_sha256 IS NOT NULL""", ns)}
        live |= {r[0] for r in db.all("""SELECT p.blob_sha256 FROM proposals p JOIN workspaces w ON w.id=p.workspace_id
                                         WHERE w.owner_id=? AND p.status='pending' AND p.blob_sha256 IS NOT NULL""", ns)}
        live |= {r[0] for r in db.all("SELECT DISTINCT received_sha256 FROM uploads WHERE owner_id=? AND state='UPLOADED'", ns)}
        for sha, path in list(ctx.blobs.iter_namespace(ns)):
            if sha in live:
                continue
            try:
                if t - path.stat().st_mtime < blob_grace:
                    continue
            except FileNotFoundError:
                continue
            ctx.blobs.delete(ns, sha)
            db.exec("DELETE FROM blobs WHERE owner_id=? AND sha256=?", ns, sha)
            stats["blobs_deleted"] += 1
        # chunks no live manifest refers to (mark and sweep)
        live_chunks: set[str] = set()
        for r in db.all("SELECT sha256 FROM blobs WHERE owner_id=? AND chunked=1", ns):
            if r["sha256"] in live:
                live_chunks.update(c for c, _ in ctx.blobs.chunks_of(ns, r["sha256"]) or [])
        for csha, path in list(ctx.blobs.iter_chunks(ns)):
            if csha in live_chunks:
                continue
            try:
                if t - path.stat().st_mtime < blob_grace:
                    continue
            except FileNotFoundError:
                continue
            path.unlink(missing_ok=True)
            if ctx.blobs.replica:
                ctx.blobs._chunk_path(ns, csha, ctx.blobs.replica).unlink(missing_ok=True)
            stats["chunks_deleted"] = stats.get("chunks_deleted", 0) + 1

    # 4. housekeeping
    for p in ctx.blobs.tmp.glob("*"):
        try:
            if t - p.stat().st_mtime > STAGING_MAX_AGE and not db.one("SELECT 1 FROM uploads WHERE staged_path=?", str(p)):
                p.unlink()
                stats["staging_removed"] += 1
        except FileNotFoundError:
            pass
    stats["idempotency_removed"] = db.exec("DELETE FROM idempotency WHERE created_at < ?", t - IDEMPOTENCY_TTL).rowcount

    # 5. integrity scrub: re-verify a rotating batch of blobs; repair from the replica
    stats.update(scrub(ctx, batch=SCRUB_BATCH))

    # 6. scheduled backup (if AGENTCLOUD_BACKUP_DIR is set)
    if data_dir is not None:
        from .backup import scheduled_backup
        try:
            b = scheduled_backup(data_dir)
            if b:
                stats["backup"] = b["path"]
        except Exception as e:
            stats["backup_error"] = f"{type(e).__name__}: {e}"
    return stats


DEDUP_BYTES_PER_PASS = 64 * 1024 * 1024


def dedup(ctx: Ctx, budget: int = DEDUP_BYTES_PER_PASS) -> dict:
    """Re-chunk whole-file blobs of at least CDC_MIN_FILE bytes, up to `budget` bytes per pass.
    Writes stay fast (stored whole); storage converges to unique chunks in the background."""
    from .storage import CDC_MIN_FILE
    db, done, used = ctx.db, 0, 0
    db.exec("UPDATE blobs SET chunked=1 WHERE chunked=0 AND size < ?", CDC_MIN_FILE)   # small files stay whole
    for r in db.all("SELECT owner_id, sha256, size FROM blobs WHERE chunked=0 ORDER BY created_at LIMIT 500"):
        if used and used + r["size"] > budget:
            break
        try:
            if ctx.blobs.rechunk(r["owner_id"], r["sha256"]):
                done += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            log(db, r["owner_id"], None, "system:worker", "dedup_failed", None, sha256=r["sha256"], error=type(e).__name__)
            continue
        used += r["size"]
        db.exec("UPDATE blobs SET chunked=1 WHERE owner_id=? AND sha256=?", r["owner_id"], r["sha256"])
    pending = db.one("SELECT COUNT(*) n FROM blobs WHERE chunked=0")["n"]
    return {"rechunked": done, "dedup_pending": pending}


SCRUB_BATCH = 500
_scrub_cursor: dict = {}


def scrub(ctx: Ctx, batch: int = SCRUB_BATCH) -> dict:
    """Decrypt-and-verify up to `batch` blobs per pass (rotating through all of them) and
    backfill the replica. Damaged blobs are repaired from the replica; unrepairable ones are
    reported in the audit log so someone can restore from a backup."""
    db = ctx.db
    rows = db.all("SELECT owner_id, sha256 FROM blobs WHERE (owner_id || sha256) > ? ORDER BY owner_id || sha256 LIMIT ?",
                  _scrub_cursor.get(id(db), ""), batch)
    _scrub_cursor[id(db)] = (rows[-1]["owner_id"] + rows[-1]["sha256"]) if len(rows) == batch else ""
    out = {"scrubbed": 0, "repaired": 0, "unrepairable": 0, "replicated": 0}
    for r in rows:
        ns, sha = r["owner_id"], r["sha256"]
        if not ctx.blobs._path(ns, sha).exists() and not ctx.blobs.exists(ns, sha):
            continue  # already garbage-collected
        out["scrubbed"] += 1
        if not ctx.blobs.verify(ns, sha):
            if ctx.blobs.repair(ns, sha):
                out["repaired"] += 1
                log(db, ns, None, "system:worker", "blob_repaired", None, sha256=sha)
            else:
                out["unrepairable"] += 1
                log(db, ns, None, "system:worker", "blob_corrupt", None, sha256=sha)
            continue
        try:
            if ctx.blobs.replicate(ns, sha):
                out["replicated"] += 1
        except OSError:
            pass
    return out


def run_forever(ctx: Ctx, interval: float = 60, data_dir=None) -> None:
    while True:
        try:
            s = run_once(ctx, data_dir=data_dir)
            if any(s.values()):
                print(f"[worker] {s}", flush=True)
        except Exception as e:  # keep going; next pass retries
            print(f"[worker] pass failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)
