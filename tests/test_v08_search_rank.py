"""Exact top-k search with proximity ranking.

Score per chunk = FTS5 BM25 (column-weighted: content 1.0, path 1.5) x (1 + ALPHA / (1 + span - k)),
where span is the smallest token window containing all k distinct matched query terms. A
file's score is its best chunk. Top-k is computed with the Threshold Algorithm: rows arrive
in BM25 order and scanning stops once no remaining row could beat the current k-th result,
which is exact because the proximity factor is bounded by (1 + ALPHA).
"""
import random

import pytest

from agentcloud_server import search_index as SI
from test_v05_agents import Modern


@pytest.fixture
def agent(server):
    dev = server.signup()
    a = dev.provision_agent("rank", access="owner", storage_mb=2048)
    return server, a, Modern(server, a["token"]["token"])


def _ws_and_p(server, a):
    from agentcloud_server.auth import Principal
    ctx = server.app.state.ctx
    ws = dict(ctx.db.one("SELECT * FROM workspaces WHERE id=?", a["workspace"]["id"]))
    return ctx, ws, Principal("agent", a["agent"]["id"], ws["owner_id"], "rank", None)


def test_proximity_ranks_adjacent_terms_first(agent):
    _, _, m = agent
    filler = " ".join(f"w{i}" for i in range(30))
    m.call("cloud_write", files=[
        {"path": "/far.md", "content": f"solar {filler} panel"},
        {"path": "/near.md", "content": f"solar panel {filler}"}])
    hits = m.call("cloud_search", query="solar panel")[0]["structuredContent"]["results"]
    assert [h["path"] for h in hits] == ["/near.md", "/far.md"]


@pytest.mark.parametrize("seed", range(6))
def test_threshold_algorithm_equals_exhaustive_ranking(agent, seed):
    server, a, _ = agent
    from agentcloud_server import service as S
    ctx, ws, p = _ws_and_p(server, a)
    rng = random.Random(seed)
    vocab = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa"] + [f"x{k}" for k in range(40)]
    for i in range(250):
        words = [rng.choice(vocab) for _ in range(rng.randint(3, 60))]
        S.write_file(ctx, p, ws, f"d{i % 7}/f{i}.md", " ".join(words).encode())
    for q in ("alpha beta", "gamma", "delta omega sigma", "kappa x3", "alpha beta gamma delta"):
        for k in (1, 5, 20):
            fast = SI.search(ctx, p, q, [ws], "", k)
            full = SI.search(ctx, p, q, [ws], "", k, exhaustive=True)
            assert [(h["path"], h["score"]) for h in fast] == [(h["path"], h["score"]) for h in full], (q, k)


def test_threshold_algorithm_stops_early(agent, monkeypatch):
    server, a, _ = agent
    from agentcloud_server import service as S
    ctx, ws, p = _ws_and_p(server, a)
    for i in range(2000):
        S.write_file(ctx, p, ws, f"n/{i}.md", f"common report text item {i}".encode())
    SI.search(ctx, p, "warm", [ws], "", 5)
    stats = {}
    SI.search(ctx, p, "common report", [ws], "", 5, stats=stats)
    assert stats["rows_scanned"] < 200          # 2,000 matching chunks, far fewer examined


def test_coverage_rule_in_the_query(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": "/a.md", "content": "red green blue"},
                                 {"path": "/b.md", "content": "red green"},
                                 {"path": "/c.md", "content": "red"}])
    paths = {h["path"] for h in m.call("cloud_search", query="red green blue")[0]["structuredContent"]["results"]}
    assert paths == {"/a.md", "/b.md"}           # 2 of 3 terms is 67% >= 60%; 1 of 3 is not


def test_scores_are_normalised(agent):
    _, _, m = agent
    m.call("cloud_write", files=[{"path": f"/s{i}.md", "content": "lynx " * (i + 1)} for i in range(5)])
    hits = m.call("cloud_search", query="lynx")[0]["structuredContent"]["results"]
    assert hits[0]["score"] == 1.0 and all(0 < h["score"] <= 1.0 for h in hits)
