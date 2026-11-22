"""Content-defined chunking (FastCDC) with chunk-level deduplication.

Writes store files whole (fast); the worker's dedup pass then splits large files at content-defined boundaries (Gear rolling hash, normalised chunking),
each chunk is stored once per organisation (encrypted, content-addressed), and a file is an
encrypted manifest of its chunks. Editing or appending to a big file stores only the chunks
that changed. Every durability feature must keep working: backups, the replica directory,
scrub and repair, and garbage collection.
"""
import os
import random
import shutil

import pytest

from agentcloud_server import service as S
from agentcloud_server.cdc import AVG, MAX, MIN, chunk_bytes
from agentcloud_server.storage import LocalEncryptedBlobStore


def _data(n, seed=0):
    rng = random.Random(seed)
    words = [bytes(f"w{rng.randrange(5000)} ", "ascii") for _ in range(2000)]
    out = bytearray()
    while len(out) < n:
        out += rng.choice(words)
    return bytes(out[:n])


def test_chunk_sizes_respect_bounds_and_average():
    data = _data(4 * 1024 * 1024)
    chunks = chunk_bytes(data)
    assert b"".join(chunks) == data
    sizes = [len(c) for c in chunks]
    assert all(MIN <= s <= MAX for s in sizes[:-1])
    avg = sum(sizes) / len(sizes)
    assert AVG * 0.6 < avg < AVG * 1.6, avg


def test_boundaries_resynchronise_after_an_insertion():
    import hashlib
    data = _data(2 * 1024 * 1024, seed=1)
    a = {hashlib.sha256(c).digest() for c in chunk_bytes(data)}
    b = {hashlib.sha256(c).digest() for c in chunk_bytes(b"INSERTED AT THE START " * 7 + data)}
    shared = len(a & b) / len(a)
    assert shared > 0.95, shared


def test_chunking_is_deterministic():
    data = _data(300_000, seed=2)
    assert chunk_bytes(data) == chunk_bytes(data)


@pytest.fixture
def env(server):
    dev = server.signup()
    a = dev.provision_agent("cdc", access="owner", storage_mb=4096)
    ctx = server.app.state.ctx
    from agentcloud_server.auth import Principal
    wsid = a["workspace"]["id"]
    owner = ctx.db.one("SELECT owner_id FROM workspaces WHERE id=?", wsid)["owner_id"]
    p = Principal("agent", a["agent"]["id"], owner, "cdc", None)

    def ws():
        return dict(ctx.db.one("SELECT * FROM workspaces WHERE id=?", wsid))
    return server, ctx, p, ws, owner


def _dedup(ctx):
    from agentcloud_server.worker import dedup
    while dedup(ctx)["dedup_pending"]:
        pass


def _physical(server, owner):
    root = server.data_dir / "blobs" / owner
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def test_appending_to_a_big_file_stores_only_new_chunks(env):
    server, ctx, p, ws, owner = env
    log = _data(1024 * 1024, seed=3)
    S.write_file(ctx, p, ws(), "logs/agent.log", log)
    _dedup(ctx)
    before = _physical(server, owner)
    for i in range(30):
        log += f"\nentry {i}: step finished ok".encode()
        S.write_file(ctx, p, ws(), "logs/agent.log", log)
    _dedup(ctx)                                    # background dedup (the worker) re-chunks new versions
    growth = _physical(server, owner) - before
    assert growth < 30 * 3 * MAX, growth          # whole copies would be ~31 MB
    f, v, data = S.read_file(ctx, p, ws(), "logs/agent.log")
    assert data == log and v["n"] == 31
    for n in (1, 15, 31):
        assert S.read_file(ctx, p, ws(), "logs/agent.log", n)[2].startswith(log[:1024 * 1024])


def test_identical_chunks_shared_across_files(env):
    server, ctx, p, ws, owner = env
    big = _data(800_000, seed=4)
    S.write_file(ctx, p, ws(), "a.txt", big)
    _dedup(ctx)
    before = _physical(server, owner)
    S.write_file(ctx, p, ws(), "b.txt", big[:400_000] + b"different tail" + big[400_000:])
    _dedup(ctx)
    assert _physical(server, owner) - before < 200_000


def test_small_files_are_stored_whole(env):
    server, ctx, p, ws, owner = env
    S.write_file(ctx, p, ws(), "small.md", b"tiny")
    sha = ctx.db.one("SELECT blob_sha256 FROM versions ORDER BY created_at DESC LIMIT 1")[0]
    assert ctx.blobs.chunks_of(owner, sha) is None
    assert S.read_file(ctx, p, ws(), "small.md")[2] == b"tiny"


def test_corrupt_chunk_is_detected_and_repaired_from_replica(tmp_path):
    key = os.urandom(32)
    store = LocalEncryptedBlobStore(tmp_path / "primary", key, replica=tmp_path / "replica")
    data = _data(700_000, seed=5)
    sha, _ = store.put_bytes("org1", data)
    assert store.chunks_of("org1", sha) is None        # stored whole on write ...
    assert store.rechunk("org1", sha)                  # ... and re-chunked in the background
    assert store.open("org1", sha) == data
    chunks = store.chunks_of("org1", sha)
    assert chunks and len(chunks) > 10
    victim = store._chunk_path("org1", chunks[3][0])
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    victim.write_bytes(bytes(raw))
    assert store.open("org1", sha) == data          # repaired from the replica during the read
    assert store.verify("org1", sha)
    store2 = LocalEncryptedBlobStore(tmp_path / "solo", key)
    sha2, _ = store2.put_bytes("org1", data)
    store2.rechunk("org1", sha2)
    c = store2._chunk_path("org1", store2.chunks_of("org1", sha2)[0][0])
    c.write_bytes(b"garbage")
    assert not store2.verify("org1", sha2)
    with pytest.raises(Exception):
        store2.open("org1", sha2)


def test_backup_and_restore_include_chunks(env, tmp_path):
    server, ctx, p, ws, owner = env
    from agentcloud_server.backup import create_backup, restore_backup, verify_backup
    data = _data(600_000, seed=6)
    S.write_file(ctx, p, ws(), "big.bin", data)
    _dedup(ctx)
    b = create_backup(server.data_dir, tmp_path / "bk")
    v = verify_backup(b["path"])
    assert v["ok"] and v["decrypted"], v
    restore_backup(b["path"], tmp_path / "restored")
    import base64
    sha = ctx.db.one("SELECT blob_sha256 FROM versions ORDER BY created_at DESC LIMIT 1")[0]
    key = base64.b64decode((server.data_dir / "master.key").read_text())
    restored = LocalEncryptedBlobStore(tmp_path / "restored" / "blobs", key)
    assert restored.open(owner, sha) == data


def test_gc_removes_orphan_chunks_but_keeps_shared_ones(env):
    server, ctx, p, ws, owner = env
    from agentcloud_server.worker import run_once
    keep = _data(500_000, seed=7)
    S.write_file(ctx, p, ws(), "keep.bin", keep)
    S.write_file(ctx, p, ws(), "drop.bin", _data(500_000, seed=8))
    _dedup(ctx)
    f = S.live_row(ctx.db, ws()["id"], "drop.bin")
    with ctx.db.tx():
        ctx.db.exec("DELETE FROM versions WHERE file_id=?", f["id"])
        ctx.db.exec("DELETE FROM files WHERE id=?", f["id"])
    chunks_before = sum(1 for _ in ctx.blobs.iter_chunks(owner))
    stats = run_once(ctx, blob_grace=0)
    assert stats["chunks_deleted"] > 0
    assert sum(1 for _ in ctx.blobs.iter_chunks(owner)) < chunks_before
    assert S.read_file(ctx, p, ws(), "keep.bin")[2] == keep
    assert stats.get("unrepairable", 0) == 0


def test_dedup_is_not_starved_by_many_small_files(env):
    """Regression: a pass over 500 small files used to report nothing done and stop."""
    server, ctx, p, ws, owner = env
    from agentcloud_server.worker import dedup
    for i in range(600):
        S.write_file(ctx, p, ws(), f"small/{i}.md", f"s{i}".encode())
    S.write_file(ctx, p, ws(), "big.log", _data(300_000, seed=11))
    out = dedup(ctx)
    assert out["rechunked"] == 1 and out["dedup_pending"] == 0
    sha = ctx.db.one("SELECT blob_sha256 FROM versions v JOIN files f ON f.id=v.file_id WHERE f.path='big.log'")[0]
    assert ctx.blobs.chunks_of(owner, sha)


def test_scrub_verifies_chunked_files(env):
    server, ctx, p, ws, owner = env
    from agentcloud_server.worker import scrub
    S.write_file(ctx, p, ws(), "s.bin", _data(400_000, seed=9))
    _dedup(ctx)
    out = scrub(ctx, batch=100)
    assert out["scrubbed"] >= 1 and out["unrepairable"] == 0
    sha = ctx.db.one("SELECT blob_sha256 FROM versions ORDER BY created_at DESC LIMIT 1")[0]
    shutil.copyfile(os.devnull, ctx.blobs._chunk_path(owner, ctx.blobs.chunks_of(owner, sha)[0][0]))
    out = scrub(ctx, batch=100)
    assert out["unrepairable"] == 1
