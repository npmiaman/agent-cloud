"""Local replica: an agent's workspace mirrored into a local SQLite file on its own machine,
kept current by an incremental sync feed, so reads and search take microseconds."""
import time

import pytest
from fastapi.testclient import TestClient

from agentcloud.replica import LocalReplica
from test_v05_agents import Modern


@pytest.fixture
def setup(server, tmp_path):
    dev = server.signup()
    a = dev.provision_agent("rep", access="owner", storage_mb=1024)
    m = Modern(server, a["token"]["token"])
    http = TestClient(server.app, base_url="http://test")

    def replica(token=None, name="r"):
        return LocalReplica(workspace=a["workspace"]["id"], token=token or a["token"]["token"], http=http,
                            path=tmp_path / f"{name}.db")
    return dev, a, m, replica


def test_sync_endpoint_full_then_incremental(server, setup):
    dev, a, m, _ = setup
    http = server.client(a["token"]["token"]).http
    ws = a["workspace"]["id"]
    m.call("cloud_write", files=[{"path": "/a.md", "content": "a"}, {"path": "/b.md", "content": "b"}])
    full = http.get(f"/v1/workspaces/{ws}/sync", params={"since": 0}).json()
    assert full["full"] and {c["path"] for c in full["changes"]} == {"a.md", "b.md"}
    seq = full["seq"]
    assert http.get(f"/v1/workspaces/{ws}/sync", params={"since": seq}).json()["changes"] == []
    m.call("cloud_write", path="/a.md", content="a2")
    m.call("cloud_delete", path="/b.md")
    m.call("cloud_move", **{"from": "/a.md", "to": "/docs/a.md"})
    delta = http.get(f"/v1/workspaces/{ws}/sync", params={"since": seq}).json()
    by = {c["file_id"]: c for c in delta["changes"]}
    assert len(by) == 2 and not delta["full"]
    moved = next(c for c in by.values() if c["path"] == "docs/a.md")
    assert moved["version"] == 2 and not moved["deleted"]
    assert any(c["deleted"] for c in by.values())
    content = http.post(f"/v1/workspaces/{ws}/sync/content", json={"files": [[moved["file_id"], 2]]}).json()
    assert content["files"][0]["content_b64"]


def test_replica_reads_in_microseconds(setup):
    _, _, m, replica = setup
    m.call("cloud_write", files=[{"path": f"/f{i}.md", "content": f"file {i}"} for i in range(20)])
    for k in range(20, 1000, 20):
        m.call("cloud_write", files=[{"path": f"/f{i}.md", "content": f"file {i}"} for i in range(k, k + 20)])
    r = replica()
    stats = r.sync()
    assert stats["fetched"] == 1000 and r.read_text("/f500.md") == "file 500"
    paths = [f"/f{i}.md" for i in range(1000)]
    t = time.perf_counter()
    for p in paths:
        r.read(p)
    cold = (time.perf_counter() - t) / len(paths)
    t = time.perf_counter()
    for p in paths:
        r.read(p)
    warm = (time.perf_counter() - t) / len(paths)
    # Wall-clock bounds on shared CI runners are noisy: cached reads must be microseconds;
    # first reads from local SQLite get a looser bound (typically ~4 µs on a laptop).
    assert warm < 50e-6, f"{warm * 1e6:.1f} µs per cached read"
    assert cold < 2e-3, f"{cold * 1e6:.1f} µs per first read"


def test_incremental_sync_fetches_only_changes(setup):
    _, _, m, replica = setup
    m.call("cloud_write", files=[{"path": f"/f{i}.md", "content": str(i)} for i in range(50)])
    r = replica()
    r.sync()
    m.call("cloud_write", path="/f3.md", content="three")
    m.call("cloud_delete", path="/f4.md")
    m.call("cloud_move", **{"from": "/f5.md", "to": "/moved/f5.md"})
    stats = r.sync()
    assert stats["fetched"] == 1 and stats["deleted"] == 1 and stats["moved"] == 1
    assert r.read_text("/f3.md") == "three"
    assert r.read("/f4.md") is None and r.read("/f5.md") is None
    assert r.read_text("/moved/f5.md") == "5"
    assert sorted(r.list("/moved/")) == ["/moved/f5.md"]


def test_replica_survives_restart(setup):
    _, _, m, replica = setup
    m.call("cloud_write", path="/keep.md", content="persisted")
    r = replica()
    r.sync()
    r.close()
    r2 = replica()                                           # same local file
    assert r2.read_text("/keep.md") == "persisted"
    assert r2.sync()["fetched"] == 0                         # resumes from its sequence number


def test_local_search_is_fast(setup):
    _, _, m, replica = setup
    for k in range(0, 2000, 50):
        m.call("cloud_write", files=[{"path": f"/n/{i}.md", "content": f"routine log entry {i}"} for i in range(k, k + 50)])
    m.call("cloud_write", path="/n/special.md", content="line one\nthe platypus venom result\nline three")
    r = replica()
    r.sync()
    hits = r.search("platypus venom")
    assert hits and hits[0]["path"] == "/n/special.md" and hits[0]["line"] == 2
    t = time.perf_counter()
    for _ in range(100):
        r.search("platypus venom")
    assert (time.perf_counter() - t) / 100 < 2e-3


def test_write_through_updates_server_and_replica(setup):
    _, _, m, replica = setup
    r = replica()
    r.sync()
    res = r.write("/w.md", "written locally")
    assert res["version"] == 1
    assert r.read_text("/w.md") == "written locally"
    assert m.call("cloud_read", path="/w.md")[1] == "written locally"
    assert r.sync()["fetched"] == 0                          # already applied


def test_revoked_access_is_removed_from_the_replica(server, setup):
    dev, a, mo, _ = setup
    other = dev.provision_agent("viewer", access="read_write")
    mo.call("cloud_write", files=[{"path": "/open/x.md", "content": "x"}, {"path": "/private/y.md", "content": "y"}])
    mo.call("cloud_permissions", action="grant", agent="viewer", path="/", access="read")
    r = LocalReplica(workspace=a["workspace"]["id"], token=other["token"]["token"],
                     http=TestClient(server.app, base_url="http://test"), path=server.data_dir.parent / "viewer.db")
    r.sync()
    assert r.read_text("/private/y.md") == "y"
    mo.call("cloud_permissions", action="deny", agent="viewer", path="/private/", access="read")
    r.sync()
    assert r.read("/private/y.md") is None and r.read_text("/open/x.md") == "x"


def test_local_reads_still_propagate_taint(setup):
    """Reads served locally never reach the server, so the replica declares them as inputs on write."""
    _, _, m, replica = setup
    m.call("cloud_write", path="/web.md", content="from a web page", sources=["https://example.com"])
    r = replica()
    r.sync()
    assert r.trust("/web.md") == "untrusted"
    r.read("/web.md")
    res = r.write("/summary.md", "summary of the page")
    assert res["trust"] == "untrusted"
    assert m.call("cloud_provenance", path="/summary.md")[0]["structuredContent"]["inputs"][0]["path"] == "/web.md"
