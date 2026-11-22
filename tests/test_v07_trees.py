"""Tree-structured commits: each commit stores a Merkle tree of content-addressed folder
objects. Unchanged folders are shared between commits, and diffs skip identical subtrees,
so diff cost tracks the size of the change, not the size of the workspace."""
import random
import time

import pytest

from agentcloud_server import service as S
from agentcloud_server import vcs as V
from agentcloud_server.auth import Principal


@pytest.fixture
def env(server):
    dev = server.signup()
    a = dev.provision_agent("tree", access="owner", storage_mb=2048)
    ctx = server.app.state.ctx
    wsid = a["workspace"]["id"]
    p = Principal("agent", a["agent"]["id"], ctx.db.one("SELECT owner_id FROM workspaces WHERE id=?", wsid)["owner_id"], "tree", None)

    def ws():
        return dict(ctx.db.one("SELECT * FROM workspaces WHERE id=?", wsid))
    return ctx, p, ws


def _random_ops(ctx, p, ws, rng, n):
    for _ in range(n):
        live = sorted(V.snapshot(ctx.db, ws()["id"]))
        op = rng.random()
        if op < 0.45 or not live:
            path = f"{rng.choice(['a', 'b', 'a/x', 'c/y/z'])}/f{rng.randrange(30)}.md"
            S.write_file(ctx, p, ws(), path, f"content {rng.randrange(5)}".encode())
        elif op < 0.6:
            S.delete_file(ctx, p, ws(), rng.choice(live))
        elif op < 0.8:
            src = rng.choice(live)
            dst = f"moved/{rng.randrange(1000)}-{src.rsplit('/', 1)[-1]}"
            if dst not in live:
                S.move(ctx, p, ws(), src, dst)
        else:
            path = rng.choice(live)
            f = S.live_row(ctx.db, ws()["id"], path)
            older = [r["n"] for r in ctx.db.all("SELECT n FROM versions WHERE file_id=? AND n < ? AND blob_sha256 IS NOT NULL",
                                                f["id"], f["current_version"])]
            if older:
                S.restore(ctx, p, ws(), path, rng.choice(older))


@pytest.mark.parametrize("seed", range(8))
def test_tree_snapshots_equal_ground_truth(env, seed):
    ctx, p, ws = env
    rng = random.Random(seed)
    commits = []
    for _ in range(6):
        _random_ops(ctx, p, ws, rng, 15)
        commits.append(V.commit(ctx, p, ws(), "c"))
    for c in commits:
        from_tree = V.tree_snapshot(ctx.db, c["id"])
        truth = V.snapshot(ctx.db, ws()["id"], c["seq"])
        assert {k: (v["file_id"], v["version"], v["sha256"]) for k, v in from_tree.items()} == \
               {k: (v["file_id"], v["version"], v["sha256"]) for k, v in truth.items()}
    for x, y in zip(commits, commits[1:]):
        assert V.compare(ctx, p, ws(), x["id"], y["id"]) == \
               V.diff(V.snapshot(ctx.db, ws()["id"], x["seq"]), V.snapshot(ctx.db, ws()["id"], y["seq"]))


def test_unchanged_folders_are_shared_between_commits(env):
    ctx, p, ws = env
    for d in range(10):
        for i in range(20):
            S.write_file(ctx, p, ws(), f"d{d}/sub/f{i}.md", f"{d}-{i}".encode())
    c1 = V.commit(ctx, p, ws(), "one")
    before = ctx.db.one("SELECT COUNT(*) n FROM trees")["n"]
    S.write_file(ctx, p, ws(), "d3/sub/f7.md", b"changed")
    c2 = V.commit(ctx, p, ws(), "two")
    new_trees = ctx.db.one("SELECT COUNT(*) n FROM trees")["n"] - before
    assert new_trees == 3                       # root, d3/, d3/sub/: the other 9 folders are reused
    assert c1["tree"] != c2["tree"]


def test_diff_cost_tracks_the_change_not_the_workspace(env, monkeypatch):
    ctx, p, ws = env
    for i in range(3000):
        S.write_file(ctx, p, ws(), f"d{i % 30}/s{i % 7}/f{i}.md", f"{i}".encode())
    c1 = V.commit(ctx, p, ws(), "base")
    assert S.live_row(ctx.db, ws()["id"], "d4/s2/f184.md")              # 184 % 30 == 4, 184 % 7 == 2
    S.write_file(ctx, p, ws(), "d4/s2/f184.md", b"edited")
    c2 = V.commit(ctx, p, ws(), "edit")
    loads = {"n": 0}
    orig = V.load_tree

    def counting(db, h):
        loads["n"] += 1
        return orig(db, h)
    monkeypatch.setattr(V, "load_tree", counting)
    t = time.perf_counter()
    d = V.compare(ctx, p, ws(), c1["id"], c2["id"])
    elapsed = time.perf_counter() - t
    assert d == {"added": [], "removed": [], "changed": ["d4/s2/f184.md"], "renamed": []}
    assert loads["n"] <= 6                      # two root-to-leaf paths, nothing else
    assert elapsed < 0.02


def test_diff_against_head_and_rollback_use_trees(env):
    ctx, p, ws = env
    for i in range(200):
        S.write_file(ctx, p, ws(), f"x/{i % 10}/f{i}.md", f"{i}".encode())
    c = V.commit(ctx, p, ws(), "base")
    S.write_file(ctx, p, ws(), "x/1/f1.md", b"changed")
    S.delete_file(ctx, p, ws(), "x/2/f2.md")
    d = V.compare(ctx, p, ws(), c["id"], None)
    assert d["changed"] == ["x/1/f1.md"] and d["removed"] == ["x/2/f2.md"]
    r = V.rollback(ctx, p, ws(), c["id"])
    assert r["written"] == ["x/1/f1.md", "x/2/f2.md"] or set(r["written"]) == {"x/1/f1.md", "x/2/f2.md"}
    assert V.compare(ctx, p, ws(), c["id"], None) == {"added": [], "removed": [], "changed": [], "renamed": []}


def test_commit_records_a_tree_quickly_at_scale(env):
    ctx, p, ws = env
    for i in range(5000):
        S.write_file(ctx, p, ws(), f"d{i % 50}/f{i}.md", f"{i}".encode())
    V.commit(ctx, p, ws(), "first")                       # builds the whole tree once
    S.write_file(ctx, p, ws(), "d7/f7.md", b"changed")
    t = time.perf_counter()
    c = V.commit(ctx, p, ws(), "second")                  # only the changed path is rebuilt
    assert time.perf_counter() - t < 0.05 and c["tree"]
