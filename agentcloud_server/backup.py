"""Backups and restore.

    agentcloud backup [--to DIR]        consistent snapshot while the server runs
    agentcloud restore BACKUP_DIR       into an empty data dir (or --force)

A backup is a directory:  agentcloud.db (SQLite online-backup API: a consistent copy
even mid-write), blobs/ (hard links when on the same filesystem: blobs are immutable,
so a link is a safe, instant, space-free copy; otherwise real copies), master.key (blobs
are encrypted with it; without it the backup is unreadable, so guard the backup dir),
and manifest.json (what's inside + verification results).

Set AGENTCLOUD_BACKUP_DIR to have the worker take one daily, keeping AGENTCLOUD_BACKUP_KEEP
(default 7).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .db import now


def _link_or_copy(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def create_backup(data_dir: Path, dest_root: Path, include_key: bool = True) -> dict:
    data_dir, dest_root = Path(data_dir), Path(dest_root)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{int(time.time() * 1000) % 1000:03d}"
    dest = dest_root / f"agentcloud-{stamp}"
    tmp = dest_root / f".partial-{stamp}"
    (tmp / "blobs").mkdir(parents=True)
    try:
        src = sqlite3.connect(data_dir / "agentcloud.db")
        out = sqlite3.connect(tmp / "agentcloud.db")
        with out:
            src.backup(out)                        # consistent snapshot, online
        src.close()
        # blobs referenced by the snapshot (so the backup is self-consistent even if GC runs meanwhile)
        refs = {(r[0], r[1]) for r in out.execute(
            """SELECT w.owner_id, v.blob_sha256 FROM versions v JOIN files f ON f.id=v.file_id JOIN workspaces w ON w.id=f.workspace_id
               WHERE v.blob_sha256 IS NOT NULL UNION SELECT w.owner_id, p.blob_sha256 FROM proposals p
               JOIN workspaces w ON w.id=p.workspace_id WHERE p.status='pending' AND p.blob_sha256 IS NOT NULL""")}
        out.close()
        modes, missing, size = {"link": 0, "copy": 0}, [], 0
        for org, sha in refs:
            s = data_dir / "blobs" / org / sha[:2] / sha
            if not s.exists():
                missing.append(f"{org}/{sha}")
                continue
            modes[_link_or_copy(s, tmp / "blobs" / org / sha[:2] / sha)] += 1
            size += s.stat().st_size
        # chunks of re-chunked files (dedup): link every chunk of each organisation in the backup.
        # Immutable and content-addressed, so a superset is safe and needs no key to compute.
        for org in {o for o, _ in refs}:
            src_chunks = data_dir / "blobs" / org / "chunks"
            if src_chunks.exists():
                for c in src_chunks.glob("*/*"):
                    if c.is_file():
                        modes[_link_or_copy(c, tmp / "blobs" / org / "chunks" / c.parent.name / c.name)] += 1
                        size += c.stat().st_size
        if include_key and (data_dir / "master.key").exists():
            shutil.copy2(data_dir / "master.key", tmp / "master.key")
            os.chmod(tmp / "master.key", 0o600)
        manifest = {"created_at": now(), "source": str(data_dir), "blobs": len(refs) - len(missing), "blob_bytes": size,
                    "linked": modes["link"], "copied": modes["copy"], "missing_blobs": missing, "includes_key": include_key,
                    "database_bytes": (tmp / "agentcloud.db").stat().st_size}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        tmp.rename(dest)                           # a backup is either complete or absent
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"path": str(dest), **manifest}


def verify_backup(path: Path, key: bytes | None = None) -> dict:
    """Check the DB opens and every referenced blob is present (and, with the key, decrypts)."""
    from .storage import LocalEncryptedBlobStore
    path = Path(path)
    db = sqlite3.connect(f"file:{path / 'agentcloud.db'}?mode=ro", uri=True)
    ok = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    refs = db.execute("""SELECT DISTINCT w.owner_id, v.blob_sha256 FROM versions v JOIN files f ON f.id=v.file_id
                         JOIN workspaces w ON w.id=f.workspace_id WHERE v.blob_sha256 IS NOT NULL""").fetchall()
    db.close()
    if key is None and (path / "master.key").exists():
        import base64
        key = base64.b64decode((path / "master.key").read_text().strip())
    store = LocalEncryptedBlobStore(path / "blobs", key) if key else None
    missing, corrupt = [], []
    for org, sha in refs:
        p = path / "blobs" / org / sha[:2] / sha
        if not p.exists():
            missing.append(sha)
        elif store:
            try:
                store.open(org, sha)
            except Exception:
                corrupt.append(sha)
    return {"database_ok": ok, "blobs_checked": len(refs), "missing": missing, "corrupt": corrupt,
            "decrypted": store is not None, "ok": ok and not missing and not corrupt}


def restore_backup(path: Path, data_dir: Path, force: bool = False) -> dict:
    path, data_dir = Path(path), Path(data_dir)
    if not (path / "agentcloud.db").exists():
        raise SystemExit(f"{path} is not an Agent Cloud backup (no agentcloud.db)")
    if data_dir.exists() and any(data_dir.iterdir()):
        if not force:
            raise SystemExit(f"{data_dir} is not empty; stop the server and pass --force to replace it")
        aside = data_dir.with_name(data_dir.name + f".before-restore-{int(time.time())}")
        data_dir.rename(aside)                     # never destroy the old data: move it aside
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path / "agentcloud.db", data_dir / "agentcloud.db")
    n = 0
    for f in (path / "blobs").rglob("*"):
        if f.is_file():
            _link_or_copy(f, data_dir / "blobs" / f.relative_to(path / "blobs"))
            n += 1
    if (path / "master.key").exists():
        shutil.copy2(path / "master.key", data_dir / "master.key")
        os.chmod(data_dir / "master.key", 0o600)
    return {"restored_to": str(data_dir), "blobs": n}


def scheduled_backup(data_dir: Path, every_s: int = 86400) -> dict | None:
    """Called by the worker: one backup per `every_s` into $AGENTCLOUD_BACKUP_DIR, pruning old ones."""
    root = os.environ.get("AGENTCLOUD_BACKUP_DIR")
    if not root:
        return None
    root_p = Path(root).expanduser()
    root_p.mkdir(parents=True, exist_ok=True)
    existing = sorted(p for p in root_p.glob("agentcloud-*") if p.is_dir())
    if existing and time.time() - existing[-1].stat().st_mtime < every_s:
        return None
    res = create_backup(data_dir, root_p)
    keep = int(os.environ.get("AGENTCLOUD_BACKUP_KEEP", "7"))
    for old in sorted(p for p in root_p.glob("agentcloud-*") if p.is_dir())[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return res
