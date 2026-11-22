"""Prolly trees for large folders.

A folder's entries are split into nodes at content-defined boundaries (a hash of the entry
name), and internal nodes index them. Properties tested:
- history independence: the same set of entries gives the same root hash however it was
  built (incremental edits == building from scratch), across random edit sequences;
- locality: changing one entry in a 20,000-entry folder writes a handful of small nodes;
- diffs load only the nodes on changed paths;
- small folders keep the original single-object format (and hashes).
"""
import hashlib
import json
import random

import pytest

from agentcloud_server import vcs as V


def entry(i, version=1):
    return {"file_id": f"fil_{i}", "version": version, "sha256": hashlib.sha256(f"{i}-{version}".encode()).hexdigest(),
            "size": 10, "content_type": "text/plain"}


@pytest.fixture
def db(server):
    return server.app.state.ctx.db


def _new_rows(db, before):
    rows = db.all("SELECT hash, length(entries) n FROM trees")
    new = [r for r in rows if r["hash"] not in before]
    return len(new), sum(r["n"] for r in new)


def test_small_folders_keep_the_single_object_format(db):
    snap = {f"f{i}.md": entry(i) for i in range(10)}
    h = V.build_tree(db, snap)
    raw = json.loads(db.one("SELECT entries FROM trees WHERE hash=?", h)["entries"])
    assert isinstance(raw, list) and len(raw) == 10


@pytest.mark.parametrize("seed", range(25))
def test_incremental_edits_equal_a_fresh_build(db, seed, monkeypatch):
    monkeypatch.setattr(V, "LEAF_TARGET", 8)          # small nodes: multi-level trees with few entries
    monkeypatch.setattr(V, "INTERNAL_TARGET", 4)
    rng = random.Random(seed)
    names = [f"dir/f{n:04d}.md" for n in range(600)]
    state = {name: entry(i) for i, name in enumerate(names[:300])}
    root = V.build_tree(db, state)
    for _ in range(12):
        edits = {}
        for _ in range(rng.randint(1, 40)):
            name = rng.choice(names)
            if name in state and rng.random() < 0.4:
                edits[name] = None
            else:
                edits[name] = entry(names.index(name), version=rng.randint(1, 5))
        for k, e in edits.items():
            if e is None:
                state.pop(k, None)
            else:
                state[k] = e
        root = V.apply_edits(db, root, edits)
        assert root == V.build_tree(db, state)          # history independence
        assert V.walk(db, root) == state                 # and the content is right


def test_changing_one_entry_in_a_huge_folder_is_local(db):
    state = {f"big/f{i:06d}.md": entry(i) for i in range(20_000)}
    root = V.build_tree(db, state)
    before = {r["hash"] for r in db.all("SELECT hash FROM trees")}
    root2 = V.apply_edits(db, root, {"big/f012345.md": entry(12345, version=2)})
    n, size = _new_rows(db, before)
    assert n <= 5 and size < 30_000, (n, size)          # a flat folder object would be ~2 MB
    assert V.walk(db, root2)["big/f012345.md"]["version"] == 2


def test_diff_in_a_huge_folder_loads_only_changed_nodes(db, monkeypatch):
    state = {f"big/f{i:06d}.md": entry(i) for i in range(20_000)}
    a = V.build_tree(db, state)
    b = V.apply_edits(db, a, {"big/f000777.md": entry(777, version=9), "big/f019999.md": None,
                              "big/new.md": entry(99999)})
    loads = {"n": 0}
    orig = V.load_tree

    def counting(d, h):
        loads["n"] += 1
        return orig(d, h)
    monkeypatch.setattr(V, "load_tree", counting)
    left, right = V.tree_diff(db, a, b)
    d = V.diff(left, right)
    assert d["changed"] == ["big/f000777.md"] and d["removed"] == ["big/f019999.md"] and d["added"] == ["big/new.md"]
    assert loads["n"] <= 16, loads["n"]


@pytest.mark.parametrize("seed", range(15))
def test_tree_diff_matches_naive_diff_on_prolly_folders(db, seed, monkeypatch):
    monkeypatch.setattr(V, "LEAF_TARGET", 8)
    monkeypatch.setattr(V, "INTERNAL_TARGET", 4)
    rng = random.Random(100 + seed)
    a_state = {f"d{i % 3}/f{i:04d}.md": entry(i) for i in range(rng.randint(50, 400))}
    b_state = dict(a_state)
    for _ in range(rng.randint(1, 30)):
        k = rng.choice(list(b_state)) if b_state and rng.random() < 0.6 else f"d{rng.randrange(4)}/n{rng.randrange(999)}.md"
        if k in b_state and rng.random() < 0.5:
            del b_state[k]
        else:
            b_state[k] = entry(rng.randrange(10**6), version=rng.randint(1, 3))
    a, b = V.build_tree(db, a_state), V.build_tree(db, b_state)
    left, right = V.tree_diff(db, a, b)
    assert V.diff(left, right) == V.diff(a_state, b_state)
