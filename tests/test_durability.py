"""Backups, restore, replica self-healing and the integrity scrub."""
import json
import subprocess
import sys
from pathlib import Path

from agentcloud_server import backup, worker


def _seed(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    for i in range(5):
        ws.write(f"docs/{i}.md", f"document {i} " * 50)
    ws.write("docs/0.md", "edited")
    return dev, ws


def test_backup_restore_roundtrip(server, tmp_path):
    from conftest import Server
    dev, ws = _seed(server)
    res = backup.create_backup(server.data_dir, tmp_path / "bk")
    assert res["blobs"] == 6 and not res["missing_blobs"] and res["linked"] == 6     # same filesystem: hard links
    assert backup.verify_backup(Path(res["path"]))["ok"]
    ws.write("docs/after-backup.md", "should not exist after restore")
    target = tmp_path / "restored"
    backup.restore_backup(Path(res["path"]), target)
    s2 = Server(target)
    tok = s2.client().http.post("/v1/auth/login", json={"email": "dev@example.com", "password": "correct-horse-battery"}).json()["token"]
    w2 = s2.client(tok).workspace(ws.id)
    assert w2.read_text("docs/0.md") == "edited" and w2.read_text("docs/0.md", version=1).startswith("document 0")
    assert not w2.exists("docs/after-backup.md")


def test_backup_survives_source_gc_and_detects_damage(server, tmp_path):
    dev, ws = _seed(server)
    res = backup.create_backup(server.data_dir, tmp_path / "bk")
    blob = next(f for f in Path(res["path"], "blobs").rglob("*") if f.is_file() and len(f.name) == 64)
    for f in (server.data_dir / "blobs").rglob("*"):                  # source loses everything
        if f.is_file() and len(f.name) == 64:
            f.unlink()
    assert backup.verify_backup(Path(res["path"]))["ok"]                 # links keep the backup intact
    blob.chmod(0o600)
    blob.write_bytes(blob.read_bytes()[:-5])                             # damage the backup copy
    v = backup.verify_backup(Path(res["path"]))
    assert not v["ok"] and v["corrupt"] == [blob.name]


def test_restore_refuses_to_clobber_and_moves_old_data_aside(server, tmp_path):
    _seed(server)
    res = backup.create_backup(server.data_dir, tmp_path / "bk")
    target = tmp_path / "existing"; target.mkdir(); (target / "keep.txt").write_text("old")
    try:
        backup.restore_backup(Path(res["path"]), target)
        assert False, "should refuse"
    except SystemExit:
        pass
    backup.restore_backup(Path(res["path"]), target, force=True)
    asides = list(tmp_path.glob("existing.before-restore-*"))
    assert asides and (asides[0] / "keep.txt").read_text() == "old"


def test_replica_heals_missing_and_corrupt_blobs(tmp_path):
    from conftest import Server
    s = Server(tmp_path / "d", replica_dir=tmp_path / "replica")
    dev = s.signup()
    ws = dev.create_workspace("w")
    data = bytes(range(256)) * 9000                                      # multi-chunk
    ws.write("big.bin", data)
    org = dev.me()["organisation"]["id"]
    sha = ws.stat("big.bin")["sha256"]
    primary = tmp_path / "d" / "blobs" / org / sha[:2] / sha
    assert (tmp_path / "replica" / org / sha[:2] / sha).exists()
    primary.unlink()                                                     # disk lost the file
    assert ws.read("big.bin") == data and primary.exists()               # served from replica, primary repaired
    raw = bytearray(primary.read_bytes()); raw[len(raw) // 2] ^= 0xFF; primary.write_bytes(bytes(raw))
    ctx = s.app.state.ctx
    stats = worker.scrub(ctx)                                            # damage deep inside: the scrub finds and fixes it
    assert stats["repaired"] == 1 and ctx.blobs.verify(org, sha)
    assert ws.read("big.bin") == data


def test_scrub_reports_unrepairable_blobs(server):
    dev, ws = _seed(server)
    ctx = server.app.state.ctx
    org = dev.me()["organisation"]["id"]
    sha = ws.stat("docs/1.md")["sha256"]
    p = server.data_dir / "blobs" / org / sha[:2] / sha
    p.write_bytes(p.read_bytes()[:-3])                                   # no replica configured
    stats = worker.scrub(ctx)
    assert stats["unrepairable"] == 1
    assert any(a["action"] == "blob_corrupt" for a in dev.activity())


def test_cli_backup_and_restore(server, tmp_path):
    _seed(server)
    run = lambda *a: subprocess.run([sys.executable, "-m", "agentcloud_server", *a], capture_output=True, text=True)
    out = run("backup", "--data", str(server.data_dir), "--to", str(tmp_path / "bk"))
    assert out.returncode == 0 and "verified ok" in out.stdout, out.stdout + out.stderr
    path = next((tmp_path / "bk").glob("agentcloud-*"))
    assert json.loads((path / "manifest.json").read_text())["blobs"] == 6
    out = run("restore", str(path), "--data", str(tmp_path / "fresh"))
    assert out.returncode == 0 and "restored 6 blobs" in out.stdout, out.stdout + out.stderr


def test_readiness_probe(server):
    http = server.client().http
    assert http.get("/readyz").json() == {"ready": True}
