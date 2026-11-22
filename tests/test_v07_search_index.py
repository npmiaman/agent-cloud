"""Persistent search index: search no longer reads every file.

Contract carried over from the scan-based search (tests/test_agent_native.py and
test_agent_interface.py still pass unchanged): BM25 ranking, 60% term coverage, matching
40-line chunk reported as `lines`, names count, deny rules respected. New guarantees:
the index is used (non-matching files aren't decrypted), results reflect writes
immediately, no plaintext lands on disk, and per-folder overviews are cheap.
"""
import time

import pytest

from test_v05_agents import Modern


@pytest.fixture
def agent(server):
    dev = server.signup()
    a = dev.provision_agent("idx", access="owner", storage_mb=2048)
    return dev, a, Modern(server, a["token"]["token"])


def _count_opens(server, monkeypatch):
    ctx = server.app.state.ctx
    calls = {"n": 0}
    orig = ctx.blobs.open

    def counting(ns, sha):
        calls["n"] += 1
        return orig(ns, sha)
    monkeypatch.setattr(ctx.blobs, "open", counting)
    return calls


def test_search_uses_the_index_not_a_scan(server, agent, monkeypatch):
    _, _, m = agent
    files = [{"path": f"/notes/n{i}.md", "content": f"ordinary note number {i} about weather"} for i in range(400)]
    files[123]["content"] = "the zebrafish regeneration experiment worked"
    for k in range(0, 400, 20):
        m.call("cloud_write", files=files[k:k + 20])
    m.call("cloud_search", query="warmup")                       # drains the index queue
    calls = _count_opens(server, monkeypatch)
    res, t = m.call("cloud_search", query="zebrafish regeneration")
    hits = res["structuredContent"]["results"]
    assert [h["path"] for h in hits] == ["/notes/n123.md"]
    assert hits[0]["lines"] == "1-1" and "zebrafish" in hits[0]["snippet"]
    assert calls["n"] <= 2                                       # only the hit is decrypted (for its snippet)


def test_results_reflect_writes_immediately(agent):
    _, _, m = agent
    m.call("cloud_write", path="/a.md", content="alpha quasar")
    assert [h["path"] for h in m.call("cloud_search", query="quasar")[0]["structuredContent"]["results"]] == ["/a.md"]
    m.call("cloud_write", path="/a.md", content="alpha pulsar")             # update replaces old terms
    assert m.call("cloud_search", query="quasar")[0]["structuredContent"]["results"] == []
    assert m.call("cloud_search", query="pulsar")[0]["structuredContent"]["results"][0]["path"] == "/a.md"
    m.call("cloud_edit", path="/a.md", edits=[{"old": "pulsar", "new": "magnetar"}])
    assert m.call("cloud_search", query="magnetar")[0]["structuredContent"]["results"][0]["version"] == 3
    m.call("cloud_move", **{"from": "/a.md", "to": "/space/a.md"})          # moves update the path
    assert m.call("cloud_search", query="magnetar")[0]["structuredContent"]["results"][0]["path"] == "/space/a.md"
    m.call("cloud_restore", path="/space/a.md", version=1)                  # restores re-index old content
    assert m.call("cloud_search", query="quasar")[0]["structuredContent"]["results"][0]["path"] == "/space/a.md"
    m.call("cloud_delete", path="/space/a.md")                              # deletes leave the index
    assert m.call("cloud_search", query="quasar")[0]["structuredContent"]["results"] == []


def test_name_matches_and_coverage_threshold(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": "/reports/kangaroo-census.md", "content": "numbers only"},
                                 {"path": "/x.md", "content": "kangaroo"},
                                 {"path": "/y.md", "content": "kangaroo census survey methodology"}])
    hits = m.call("cloud_search", query="kangaroo census")[0]["structuredContent"]["results"]
    paths = [h["path"] for h in hits]
    assert set(paths) == {"/reports/kangaroo-census.md", "/y.md"}           # /x.md covers 1 of 2 terms (< 60%)
    named = next(h for h in hits if h["path"] == "/reports/kangaroo-census.md")
    assert "name" in named["matched_in"]


def test_search_respects_permissions(server, agent):
    dev, owner, mo = agent
    mo.call("cloud_write", files=[{"path": "/public/p.md", "content": "shared ocelot facts"},
                                  {"path": "/secret/s.md", "content": "hidden ocelot facts"}])
    other = dev.provision_agent("reader", access="read_write")
    mx = Modern(server, other["token"]["token"])
    assert mx.call("cloud_search", query="ocelot")[0]["structuredContent"]["results"] == []      # no access at all
    mo.call("cloud_permissions", action="grant", agent="reader", path="/", access="read")
    mo.call("cloud_permissions", action="deny", agent="reader", path="/secret/", access="read")
    paths = [h["path"] for h in mx.call("cloud_search", query="ocelot")[0]["structuredContent"]["results"]]
    assert paths == ["/public/p.md"]


def test_no_plaintext_terms_on_disk(server, agent):
    _, _, m = agent
    m.call("cloud_write", path="/diary.md", content="xylophonist marmalade confidential")
    m.call("cloud_search", query="xylophonist")
    ctx = server.app.state.ctx
    ctx.db.exec("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in server.data_dir.glob("agentcloud.db*"))
    assert b"xylophonist" not in raw and b"marmalade" not in raw


def test_branch_and_merge_content_is_searchable(agent):
    _, _, m = agent
    m.call("cloud_write", path="/plan.md", content="initial plan")
    br = m.call("cloud_branch", action="create", name="s")[0]["structuredContent"]["branch"]
    assert m.call("cloud_search", query="initial", workspace=br)[0]["structuredContent"]["results"]
    m.call("cloud_write", path="/plan.md", content="revised narwhal plan", workspace=br)
    m.call("cloud_branch", action="merge", workspace=br)
    hits = m.call("cloud_search", query="narwhal", workspace="idx")[0]["structuredContent"]["results"]
    assert [h["path"] for h in hits] == ["/plan.md"]


def test_index_backfills_existing_files(server, agent):
    """Files written before the index existed (or a wiped index) are indexed on the next search."""
    _, _, m = agent
    m.call("cloud_write", path="/old.md", content="ancient axolotl lore")
    ctx = server.app.state.ctx
    with ctx.db.tx():
        ctx.db.exec("DELETE FROM search_chunks")
        ctx.db.exec("DELETE FROM search_files")
    assert [h["path"] for h in m.call("cloud_search", query="axolotl")[0]["structuredContent"]["results"]] == ["/old.md"]


def test_search_latency_stays_flat_as_the_workspace_grows(server, agent):
    _, a, m = agent
    from agentcloud_server import service as S
    from agentcloud_server.auth import Principal
    ctx = server.app.state.ctx
    ws = dict(ctx.db.one("SELECT * FROM workspaces WHERE id=?", a["workspace"]["id"]))
    p = Principal("agent", a["agent"]["id"], ws["owner_id"], "idx", None)

    def timed(q):
        m.call("cloud_search", query=q)
        t = time.perf_counter()
        for _ in range(10):
            m.call("cloud_search", query=q)
        return (time.perf_counter() - t) / 10

    for i in range(300):
        S.write_file(ctx, p, ws, f"a/{i}.md", f"common words file {i}".encode())
    S.write_file(ctx, p, ws, "needle.md", b"rare okapi sighting")
    small = timed("okapi")
    for i in range(300, 3000):
        S.write_file(ctx, p, ws, f"b/{i % 50}/{i}.md", f"common words file {i}".encode())
    large = timed("okapi")
    assert large < max(small * 3, 0.02), (small, large)          # 10x the files, not 10x the time


def test_overview_summarises_folders(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": "/data/raw/a.csv", "content": "x" * 100},
                                 {"path": "/data/raw/b.csv", "content": "y" * 50},
                                 {"path": "/data/clean/c.csv", "content": "z" * 10},
                                 {"path": "/notes/todo.md", "content": "t"}])
    res, t = m.call("cloud_overview", path="/", depth=2)
    o = res["structuredContent"]
    assert o["files"] == 4 and o["bytes"] == 161
    data = next(f for f in o["folders"] if f["path"] == "/data/")
    assert data["files"] == 3 and data["bytes"] == 160
    raw = next(f for f in data["folders"] if f["path"] == "/data/raw/")
    assert raw["files"] == 2 and raw["bytes"] == 150
    assert "/data/" in t and "3 files" in t
    res, _ = m.call("cloud_overview", path="/data/raw/")
    assert res["structuredContent"]["files"] == 2 and [x["path"] for x in res["structuredContent"]["recent"]][:2]
