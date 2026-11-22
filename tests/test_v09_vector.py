"""Vector search: pluggable embedders, an HNSW index, encrypted vector storage, and hybrid
(keyword + vector) ranking with Reciprocal Rank Fusion.

No model is downloaded by these tests. The built-in HashingEmbedder (feature hashing of words
and character trigrams) stands in for a real model: it is lexical, not semantic, and the
tests only rely on that. Real-model providers are tested with fakes.
"""
import math
import random
import sys

import httpx
import pytest

from agentcloud_server import embeddings as EM
from agentcloud_server.hnsw import HNSW
from test_v05_agents import Modern


# ------------------------------------------------------------------ embedders

def test_hashing_embedder_is_deterministic_and_normalised():
    e = EM.HashingEmbedder()
    a, b = e.embed(["quarterly revenue report", "quarterly revenue report"])
    assert a == b and len(a) == e.dim
    assert abs(math.sqrt(sum(x * x for x in a)) - 1.0) < 1e-6


def test_hashing_embedder_similarity_tracks_shared_wording():
    e = EM.HashingEmbedder()
    q, near, far = e.embed(["revenue forecast for next quarter", "next quarter revenue forecasting", "kitten photos"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))   # noqa: E731
    assert dot(q, near) > 0.5 > dot(q, far)


def test_local_model_never_downloads_by_default(monkeypatch):
    calls = {}

    class FakeST:
        def __init__(self, name, **kw):
            calls["name"], calls["kw"] = name, kw

        def get_sentence_embedding_dimension(self):
            return 3

        def encode(self, texts, normalize_embeddings=False, **kw):
            assert normalize_embeddings
            return [[1.0, 0.0, 0.0] for _ in texts]
    fake_mod = type(sys)("sentence_transformers")
    fake_mod.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    e = EM.from_config("sentence-transformers:all-MiniLM-L6-v2")
    assert "kw" not in calls                                   # nothing loads until first use
    assert e.embed(["x"]) == [[1.0, 0.0, 0.0]] and e.dim == 3
    assert calls["name"] == "all-MiniLM-L6-v2" and calls["kw"]["local_files_only"] is True
    e2 = EM.from_config("sentence-transformers:other-model", allow_download=True)
    e2.embed(["x"])
    assert calls["kw"]["local_files_only"] is False


def test_importing_the_server_does_not_import_model_libraries():
    import importlib
    import subprocess
    out = subprocess.run([sys.executable, "-c", "import sys; import agentcloud_server.app, agentcloud_server.embeddings; "
                          "print('sentence_transformers' in sys.modules, 'torch' in sys.modules)"],
                         capture_output=True, text=True, cwd=str(importlib.import_module("agentcloud_server").__path__[0] + "/.."))
    assert out.stdout.strip() == "False False", out.stderr


def test_http_embedder_speaks_the_openai_embeddings_format():
    seen = {}

    def handler(request: httpx.Request):
        import json
        body = json.loads(request.content)
        seen["auth"], seen["body"] = request.headers.get("authorization"), body
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [3.0, 4.0]} for i, _ in enumerate(body["input"])]})
    e = EM.HTTPEmbedder("http://embed.local/v1/embeddings", model="text-embed", api_key="k123",
                        http=httpx.Client(transport=httpx.MockTransport(handler)))
    vecs = e.embed(["a", "b"])
    assert vecs == [[0.6, 0.8], [0.6, 0.8]] and e.dim == 2
    assert seen["auth"] == "Bearer k123" and seen["body"] == {"model": "text-embed", "input": ["a", "b"]}


def test_unknown_or_empty_config():
    assert EM.from_config(None) is None and EM.from_config("none") is None
    assert isinstance(EM.from_config("hashing"), EM.HashingEmbedder)
    with pytest.raises(ValueError):
        EM.from_config("magic:thing")


# ------------------------------------------------------------------ HNSW

def _clustered(n, dim, seed):
    rng = random.Random(seed)
    centers = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(20)]
    out = []
    for _ in range(n):
        c = rng.choice(centers)
        v = [x + rng.gauss(0, 0.35) for x in c]
        norm = math.sqrt(sum(x * x for x in v))
        out.append([x / norm for x in v])
    return out


def _brute(vecs, q, k, allowed=None):
    scored = [(sum(a * b for a, b in zip(v, q)), i) for i, v in enumerate(vecs) if allowed is None or allowed(i)]
    return [i for _, i in sorted(scored, reverse=True)[:k]]


@pytest.mark.parametrize("use_numpy", [True, False])
def test_hnsw_recall(use_numpy):
    n = 3000 if use_numpy else 600
    vecs = _clustered(n, 48, seed=1)
    idx = HNSW(48, seed=7, use_numpy=use_numpy)
    for i, v in enumerate(vecs):
        idx.add(i, v)
    queries = _clustered(60, 48, seed=2)
    hits = total = 0
    for q in queries:
        truth = set(_brute(vecs, q, 10))
        got = {i for i, _ in idx.search(q, 10)}
        hits += len(truth & got)
        total += 10
    assert hits / total >= 0.95, hits / total


def test_hnsw_delete_update_and_rebuild():
    vecs = _clustered(1500, 32, seed=3)
    idx = HNSW(32, seed=1)
    for i, v in enumerate(vecs):
        idx.add(i, v)
    gone = set(range(0, 1500, 2))
    for i in gone:
        idx.remove(i)
    assert len(idx) == 750
    for q in _clustered(30, 32, seed=4):
        got = [i for i, _ in idx.search(q, 10)]
        assert not set(got) & gone
        truth = set(_brute(vecs, q, 10, allowed=lambda i: i not in gone))
        assert len(truth & set(got)) >= 9
    idx.add(1, vecs[0])                                # update: id 1 now has vector 0
    assert idx.search(vecs[0], 1)[0][0] in (0, 1) and 0 in gone and idx.search(vecs[0], 1)[0][0] == 1


def test_hnsw_filtered_search_respects_the_filter_and_fills_k():
    vecs = _clustered(2000, 32, seed=5)
    idx = HNSW(32, seed=2)
    for i, v in enumerate(vecs):
        idx.add(i, v)
    allowed = lambda i: i % 10 == 3                       # noqa: E731  (10% of the points)
    for q in _clustered(20, 32, seed=6):
        got = [i for i, _ in idx.search(q, 10, allow=allowed)]
        assert len(got) == 10 and all(allowed(i) for i in got)
        assert set(got) == set(_brute(vecs, q, 10, allowed=allowed))   # exact fallback when the filter is selective


# ------------------------------------------------------------------ integration

@pytest.fixture
def vserver(tmp_path):
    from conftest import Server
    return Server(tmp_path / "data", embedder="hashing")


@pytest.fixture
def agent(vserver):
    dev = vserver.signup()
    a = dev.provision_agent("vec", access="owner", storage_mb=512)
    return vserver, dev, a, Modern(vserver, a["token"]["token"])


def test_semantic_search_end_to_end(agent):
    _, _, _, m = agent
    m.call("cloud_write", files=[{"path": "/notes/finance.md", "content": "Revenue forecasting for the next quarter"},
                                 {"path": "/notes/pets.md", "content": "Photos of kittens and puppies"}])
    res, t = m.call("cloud_search", query="quarterly revenue forecast", mode="semantic")
    hits = res["structuredContent"]["results"]
    assert hits[0]["path"] == "/notes/finance.md" and hits[0]["matched_in"] == "semantic"
    assert res["structuredContent"]["mode"] == "semantic"


def test_hybrid_fuses_keyword_and_vector_results(agent):
    _, _, _, m = agent
    m.call("cloud_write", files=[{"path": "/a.md", "content": "zebrafish regeneration protocol"},
                                 {"path": "/b.md", "content": "regenerating zebra fishes in the lab"},
                                 {"path": "/c.md", "content": "unrelated gardening tips"}])
    res, _ = m.call("cloud_search", query="zebrafish regeneration", mode="hybrid")
    paths = [h["path"] for h in res["structuredContent"]["results"]]
    assert paths[0] == "/a.md" and "/b.md" in paths and "/c.md" not in paths[:2]
    a = res["structuredContent"]["results"][0]
    assert "keyword" in a["matched_in"] and "semantic" in a["matched_in"]


def test_vectors_follow_writes_updates_and_deletes(agent):
    _, _, _, m = agent
    m.call("cloud_write", path="/x.md", content="marine biology field report")
    assert m.call("cloud_search", query="marine biology", mode="semantic")[0]["structuredContent"]["results"][0]["path"] == "/x.md"
    m.call("cloud_write", path="/x.md", content="astronomy observation log")
    hits = m.call("cloud_search", query="astronomy observation", mode="semantic")[0]["structuredContent"]["results"]
    assert hits[0]["path"] == "/x.md" and hits[0]["version"] == 2
    m.call("cloud_delete", path="/x.md")
    assert all(h["path"] != "/x.md" for h in m.call("cloud_search", query="astronomy", mode="semantic")[0]["structuredContent"]["results"])


def test_semantic_results_respect_permissions(agent):
    vserver, dev, owner, mo = agent
    mo.call("cloud_write", files=[{"path": "/open/a.md", "content": "volcano eruption study"},
                                  {"path": "/secret/b.md", "content": "volcano eruption classified"}])
    other = dev.provision_agent("reader", access="read_write")
    mx = Modern(vserver, other["token"]["token"])
    mo.call("cloud_permissions", action="grant", agent="reader", path="/", access="read")
    mo.call("cloud_permissions", action="deny", agent="reader", path="/secret/", access="read")
    paths = [h["path"] for h in mx.call("cloud_search", query="volcano eruption", mode="semantic")[0]["structuredContent"]["results"]]
    assert paths == ["/open/a.md"]


def test_vectors_are_encrypted_at_rest(agent):
    vserver, _, _, m = agent
    m.call("cloud_write", path="/v.md", content="encrypted vectors please")
    m.call("cloud_search", query="encrypted", mode="semantic")
    ctx = vserver.app.state.ctx
    row = ctx.db.one("SELECT vec FROM vector_chunks LIMIT 1")
    import struct
    plain = struct.pack(f"{EM.HashingEmbedder().dim}f", *EM.HashingEmbedder().embed(["/v.md\nencrypted vectors please"])[0])
    assert plain[:64] not in bytes(row["vec"]) and len(bytes(row["vec"])) > len(plain)


def test_a_second_process_sees_new_vectors(agent, tmp_path):
    vserver, _, a, m = agent
    from conftest import Server
    other = Server(vserver.data_dir, embedder="hashing")            # same data dir, separate app (another process)
    m2 = Modern(other, a["token"]["token"])
    m2.call("cloud_search", query="warm", mode="semantic")           # builds its in-memory index
    m.call("cloud_write", path="/late.md", content="hydroponic lettuce yields")
    m.call("cloud_search", query="hydroponic", mode="semantic")      # first process embeds it
    hits = m2.call("cloud_search", query="hydroponic lettuce", mode="semantic")[0]["structuredContent"]["results"]
    assert hits and hits[0]["path"] == "/late.md"


def test_without_an_embedder(server):
    dev = server.signup()
    a = dev.provision_agent("plain", access="owner")
    m = Modern(server, a["token"]["token"])
    m.call("cloud_write", path="/k.md", content="keyword only")
    res, t = m.call("cloud_search", query="keyword", mode="semantic")
    assert res["isError"] and "not configured" in t
    res, _ = m.call("cloud_search", query="keyword")                  # auto -> keyword
    assert res["structuredContent"]["mode"] == "keyword" and res["structuredContent"]["results"]


def test_changing_the_model_reembeds(agent, monkeypatch):
    vserver, _, _, m = agent
    m.call("cloud_write", path="/r.md", content="reindex me")
    m.call("cloud_search", query="reindex", mode="semantic")
    ctx = vserver.app.state.ctx
    assert ctx.db.one("SELECT COUNT(*) n FROM vector_chunks WHERE model=?", "hashing-384-v1")["n"] >= 1

    class Hash2(EM.HashingEmbedder):
        name = "hashing-384-v2"
    monkeypatch.setattr(ctx, "embedder", Hash2())
    hits = m.call("cloud_search", query="reindex", mode="semantic")[0]["structuredContent"]["results"]
    assert hits and ctx.db.one("SELECT COUNT(*) n FROM vector_chunks WHERE model=?", "hashing-384-v2")["n"] >= 1


def test_status_endpoint(agent):
    vserver, dev, a, m = agent
    m.call("cloud_write", path="/s.md", content="status")
    r = dev.http.get("/v1/vector/status")
    assert r.status_code == 200 and r.json()["embedder"] == "hashing-384-v1" and "pending" in r.json()
