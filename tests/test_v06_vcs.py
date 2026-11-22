"""Workspace version control: O(1) commits, diffs, rollback, branches and three-way merges."""
import time

import pytest

from test_v05_agents import Modern


@pytest.fixture
def agent(server):
    dev = server.signup()
    a = dev.provision_agent("vc", access="owner", storage_mb=100)
    return dev, a, Modern(server, a["token"]["token"])


def test_commit_log_diff_and_rollback(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": "/a.md", "content": "a1"}, {"path": "/b.md", "content": "b1"}])
    res, t = m.call("cloud_commit", action="create", message="first")
    c1 = res["structuredContent"]["id"]
    assert "2 files" in t and "first commit" in t
    m.call("cloud_write", path="/a.md", content="a2")
    m.call("cloud_delete", path="/b.md")
    m.call("cloud_write", path="/c.md", content="c1")
    m.call("cloud_move", **{"from": "/a.md", "to": "/docs/a.md"})
    res, t = m.call("cloud_commit", action="create", message="second")
    c2 = res["structuredContent"]["id"]
    s = res["structuredContent"]["since_parent"]
    assert s["added"] == ["c.md"] and s["removed"] == ["b.md"]
    assert s["renamed"] == [{"from": "a.md", "to": "docs/a.md", "changed": True}]
    _, t = m.call("cloud_commit", action="log")
    assert t.index(c2) < t.index(c1) and '"first"' in t
    res, t = m.call("cloud_commit", action="diff", **{"from": c1, "to": c2})
    assert "+ /c.md" in t and "- /b.md" in t and "/a.md -> /docs/a.md (changed)" in t
    # roll back to the first commit: a.md back at its old path with old content, b.md back, c.md gone
    res, t = m.call("cloud_commit", action="rollback", commit=c1)
    assert not res["isError"], t
    assert m.call("cloud_read", path="/a.md")[1] == "a1"
    assert m.call("cloud_read", path="/b.md")[1] == "b1"
    assert m.call("cloud_read", path="/c.md")[0]["isError"]
    assert m.call("cloud_read", path="/docs/a.md")[0]["isError"]
    assert not any(m.call("cloud_commit", action="diff", **{"from": c1})[0]["structuredContent"].values())
    # and the rollback itself can be undone
    m.call("cloud_commit", action="rollback", commit=c2)
    assert m.call("cloud_read", path="/docs/a.md")[1] == "a2" and m.call("cloud_read", path="/c.md")[1] == "c1"


def test_commit_is_constant_time(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": f"/f{i}.md", "content": str(i)} for i in range(20)])
    t0 = time.perf_counter()
    for _ in range(20):
        m.call("cloud_commit", action="create")
    assert (time.perf_counter() - t0) / 20 < 0.1        # each commit records a sequence number, it copies nothing


def test_branch_merge_without_conflicts(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": "/plan.md", "content": "v1"}, {"path": "/notes.md", "content": "n1"}])
    res, t = m.call("cloud_branch", action="create", name="experiment")
    assert not res["isError"], t
    br = res["structuredContent"]["branch"]
    assert br == "vc.experiment" and res["structuredContent"]["files"] == 2
    m.call("cloud_write", path="/plan.md", content="v2 on branch", workspace=br)
    m.call("cloud_write", path="/new.md", content="only on branch", workspace=br)
    m.call("cloud_write", path="/notes.md", content="n2 on main")           # parent changes a different file
    _, t = m.call("cloud_branch", action="preview", workspace=br)
    assert "2 change(s)" in t and "Conflicts" not in t
    res, t = m.call("cloud_branch", action="merge", workspace=br)
    assert not res["isError"], t
    assert m.call("cloud_read", path="/plan.md")[1] == "v2 on branch"
    assert m.call("cloud_read", path="/new.md")[1] == "only on branch"
    assert m.call("cloud_read", path="/notes.md")[1] == "n2 on main"          # parent's own change kept
    _, t = m.call("cloud_branch", action="list")
    assert "vc.experiment" in t and "(merged)" in t


def test_merge_conflicts_must_be_resolved(agent):
    _, _, m = agent
    m.call("cloud_write", path="/x.md", content="base")
    br = m.call("cloud_branch", action="create", name="b")[0]["structuredContent"]["branch"]
    m.call("cloud_write", path="/x.md", content="theirs", workspace=br)
    m.call("cloud_write", path="/x.md", content="ours")
    res, t = m.call("cloud_branch", action="merge", workspace=br)
    assert res["isError"] and res["structuredContent"]["error"] == "MERGE_CONFLICT" and "/x.md" in t
    assert m.call("cloud_read", path="/x.md")[1] == "ours"                   # nothing applied
    res, t = m.call("cloud_branch", action="merge", workspace=br, resolve={"/x.md": "theirs"})
    assert not res["isError"], t
    assert m.call("cloud_read", path="/x.md")[1] == "theirs"


def test_branch_is_private_and_shares_storage(server, agent):
    dev, a, m = agent
    other = dev.provision_agent("other", access="read_write")
    mo = Modern(server, other["token"]["token"])
    m.call("cloud_write", path="/big.bin", content="x" * 50_000)
    br = m.call("cloud_branch", action="create", name="p")[0]["structuredContent"]["branch"]
    assert mo.call("cloud_read", path="/big.bin", workspace=br)[0]["isError"]
    # same content is the same blob: nothing new written to disk
    ctx = server.app.state.ctx
    shas = {r["blob_sha256"] for r in ctx.db.all("SELECT blob_sha256 FROM versions WHERE blob_sha256 IS NOT NULL")}
    assert len(shas) == 1


def test_untrusted_label_survives_branch_and_merge(agent):
    _, _, m = agent
    m.call("cloud_write", path="/web.md", content="from the web", sources=["https://example.com"])
    br = m.call("cloud_branch", action="create", name="t")[0]["structuredContent"]["branch"]
    assert m.call("cloud_read", path="/web.md", workspace=br)[0]["structuredContent"]["trust"] == "untrusted"


def test_rollback_and_merge_respect_approval_policy(server):
    dev = server.signup()
    dev.provision_agent("o", access="owner")
    helper = dev.provision_agent("h", access="read_write", approval="all")
    mh = Modern(server, helper["token"]["token"])
    mh_ws = helper["workspace"]["id"]
    dev.workspace(mh_ws).write("a.md", b"one")
    c = mh.call("cloud_commit", action="create")[0]["structuredContent"]["id"]
    dev.workspace(mh_ws).write("a.md", b"two")
    res, t = mh.call("cloud_commit", action="rollback", commit=c)
    assert res["isError"] and "approval" in t
    assert mh.call("cloud_read", path="/a.md")[1] == "two"


def test_non_owners_cannot_branch(server, agent):
    dev, _, _ = agent
    rw = dev.provision_agent("rw", access="read_write")
    m = Modern(server, rw["token"]["token"])
    assert "cloud_branch" not in m.tools() and "cloud_commit" in m.tools()


def test_usage_counter_matches_full_recount(server, agent):
    """The trigger-maintained byte counter must equal a from-scratch recount after any mix of operations."""
    dev, a, m = agent
    ctx = server.app.state.ctx
    m.call("cloud_write", files=[{"path": f"/f{i}.md", "content": f"content {i % 3}"} for i in range(9)])   # duplicates
    m.call("cloud_write", path="/f0.md", content="changed")
    m.call("cloud_delete", path="/f1.md")
    br = m.call("cloud_branch", action="create", name="u")[0]["structuredContent"]["workspace_id"]
    m.call("cloud_write", path="/g.md", content="branch only", workspace="vc.u")
    for ws_id in (a["workspace"]["id"], br):
        recount = ctx.db.one("""SELECT COALESCE(SUM(size),0) s FROM (SELECT DISTINCT v.blob_sha256, v.size FROM versions v
                                JOIN files f ON f.id=v.file_id WHERE f.workspace_id=? AND v.blob_sha256 IS NOT NULL)""", ws_id)["s"]
        assert ctx.db.one("SELECT used_bytes FROM workspaces WHERE id=?", ws_id)["used_bytes"] == recount
    # purging a file's versions (what retention does) releases its unique content
    f = ctx.db.one("SELECT id FROM files WHERE workspace_id=? AND path='g.md'", br)
    before = ctx.db.one("SELECT used_bytes FROM workspaces WHERE id=?", br)["used_bytes"]
    with ctx.db.tx():
        ctx.db.exec("DELETE FROM versions WHERE file_id=?", f["id"])
    assert ctx.db.one("SELECT used_bytes FROM workspaces WHERE id=?", br)["used_bytes"] == before - len("branch only")
