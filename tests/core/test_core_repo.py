"""Repository basics: objects, chunking, trees, commits, encryption (SPEC §2-§6)."""
import os
import random

import pytest

from agentcloud_core import Conflict, MemoryStore, Repo


def _text(n, seed=0):
    rng = random.Random(seed)
    words = [f"w{rng.randrange(3000)} " for _ in range(1500)]
    out = []
    size = 0
    while size < n:
        w = rng.choice(words)
        out.append(w)
        size += len(w)
    return "".join(out)[:n].encode()


@pytest.fixture
def repo():
    return Repo.init(MemoryStore(), "r", key=os.urandom(32))


def test_write_commit_read(repo):
    b = repo.branch("agents/a1")
    tx = b.begin(writer="agent:a1")
    p = tx.write("notes/plan.md", b"hello")
    c = tx.commit("first")
    assert b.read("notes/plan.md") == b"hello"
    assert b.entry("notes/plan.md")["prov"] == p
    assert repo.prov(p)["writer"] == "agent:a1" and repo.prov(p)["path"] == "notes/plan.md"
    assert [x["id"] for x in repo.log("agents/a1")] == [c]
    tx = b.begin(writer="agent:a1")
    tx.write("notes/plan.md", b"hello v2")
    tx.delete("missing.md")
    c2 = tx.commit("second")
    assert b.read("notes/plan.md") == b"hello v2" and b.read("notes/plan.md", at=c) == b"hello"
    assert repo.diff(c, c2) == {"added": [], "removed": [], "changed": ["notes/plan.md"]}


def test_reopen_and_descriptor(repo):
    b = repo.branch("main")
    tx = b.begin()
    tx.write("x.txt", b"persist")
    tx.commit("x")
    again = Repo.open(repo.store, "r", key=repo.key)
    assert again.branch("main").read("x.txt") == b"persist"
    assert again.descriptor["format"] == "agentcloud-repo" and again.descriptor["chunking"]["avg"] == 8192
    with pytest.raises(Exception):
        Repo.open(repo.store, "r", key=os.urandom(32)).branch("main").read("x.txt")   # wrong key


def test_large_files_are_chunked_and_deduplicated(repo):
    b = repo.branch("main")
    data = _text(1_000_000)
    tx = b.begin()
    tx.write("log.txt", data)
    tx.commit("v1")
    before = len(repo.store.list("r/objects/"))
    for i in range(10):
        data += f"\nappend {i}".encode()
        tx = b.begin()
        tx.write("log.txt", data)
        tx.commit(f"v{i + 2}")
    new_objects = len(repo.store.list("r/objects/")) - before
    assert new_objects < 10 * 8, new_objects        # a handful of chunks + metadata per append, not 120 chunks each
    assert b.read("log.txt") == data


def test_history_independent_trees(repo):
    a, b = repo.branch("a"), repo.branch("b")
    names = [f"d/f{i:03d}.md" for i in range(300)]
    tx = a.begin()
    for n in names:
        tx.write(n, n.encode(), deterministic=True)
    tx.commit("all at once")
    for chunk in (names[150:], names[:150]):
        tx = b.begin()
        for n in reversed(chunk):
            tx.write(n, n.encode(), deterministic=True)
        tx.commit("in pieces")
    assert repo.commit(a.head())["tree"] == repo.commit(b.head())["tree"]


def test_encrypted_at_rest_with_keyed_ids():
    store = MemoryStore()
    r1 = Repo.init(store, "one", key=os.urandom(32))
    r2 = Repo.init(store, "two", key=os.urandom(32))
    for r in (r1, r2):
        tx = r.branch("main").begin()
        tx.write("secret.txt", b"the launch code is swordfish")
        tx.commit("s")
    raw = b"".join(store.get(k)[0] for k in store.list(""))
    assert b"swordfish" not in raw
    e1, e2 = r1.branch("main").entry("secret.txt"), r2.branch("main").entry("secret.txt")
    assert e1["blob"] != e2["blob"]                  # same content, different repositories: unlinkable ids


def test_single_writer_branch_detects_a_race(repo):
    b = repo.branch("agents/solo")
    t1, t2 = b.begin(), b.begin()
    t1.write("a.md", b"1")
    t2.write("b.md", b"2")
    t1.commit("one")
    with pytest.raises(Conflict):
        t2.commit("two")                             # shared=False: a moved head is an error


def test_unencrypted_repositories_work_too():
    r = Repo.init(MemoryStore(), "plain", key=None)
    tx = r.branch("main").begin()
    tx.write("a.txt", b"plain")
    tx.commit("p")
    assert r.branch("main").read("a.txt") == b"plain" and r.descriptor["encryption"]["scheme"] == "none"
