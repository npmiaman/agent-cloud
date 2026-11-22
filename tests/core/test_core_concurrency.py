"""Many writers: shared branches with compare-and-swap retries, per-agent branches, and
hierarchical merges (worker -> team -> project), including fan-in merges (SPEC §6-§7)."""
import os
import threading

import pytest

from agentcloud_core import LocalStore, MemoryStore, MergeConflict, Repo


@pytest.fixture(params=["memory", "local"])
def repo(request, tmp_path):
    store = MemoryStore() if request.param == "memory" else LocalStore(tmp_path / "store")
    return Repo.init(store, "r", key=os.urandom(32))


def test_shared_branch_has_no_lost_updates(repo):
    b = repo.branch("shared")
    errors = []

    def agent(n):
        try:
            for k in range(8):
                tx = b.begin(writer=f"agent:{n}", shared=True)
                tx.write(f"out/{n}-{k}.md", f"{n}-{k}".encode())
                tx.commit(f"{n}-{k}")
        except Exception as e:      # pragma: no cover - surfaced below
            errors.append(e)
    threads = [threading.Thread(target=agent, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    files = b.files()
    assert len(files) == 64 and all(b.read(f"out/{n}-{k}.md") == f"{n}-{k}".encode() for n in range(8) for k in range(8))
    assert len(repo.log("shared", limit=1000)) == 64


def test_hierarchical_merges_carry_every_agents_work(repo):
    teams = {f"team{t}": [f"agents/t{t}w{w}" for w in range(5)] for t in range(3)}

    def worker(name):
        b = repo.branch(name)
        for k in range(3):
            tx = b.begin(writer=name)
            tx.write(f"{name}/r{k}.md", f"{name} result {k}".encode())
            tx.commit(f"{name} {k}")
    threads = [threading.Thread(target=worker, args=(w,)) for ws in teams.values() for w in ws]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for team, workers in teams.items():
        repo.merge_many(workers, f"teams/{team}")                 # one fan-in commit per team
    repo.merge_many([f"teams/{t}" for t in teams], "project")
    files = repo.branch("project").files()
    assert len(files) == 3 * 5 * 3
    head = repo.commit(repo.branch("project").head())
    assert len(head["parents"]) == 3                              # new branch: one fan-in commit over the 3 team heads
    for team, workers in teams.items():
        for w in workers:
            assert repo.branch("project").read(f"{w}/r2.md") == f"{w} result 2".encode()


def test_repeated_merges_only_bring_new_work(repo):
    w, team = repo.branch("agents/w"), "teams/t"
    tx = w.begin()
    tx.write("a.md", b"1")
    tx.commit("1")
    repo.merge("agents/w", team)
    tx = w.begin()
    tx.write("b.md", b"2")
    tx.commit("2")
    tx = repo.branch(team).begin()
    tx.write("team-notes.md", b"team")
    tx.commit("team")
    repo.merge("agents/w", team)
    assert set(repo.branch(team).files()) == {"a.md", "b.md", "team-notes.md"}


def test_text_edits_to_the_same_file_merge_line_by_line(repo):
    base = "".join(f"line {i}\n" for i in range(20)).encode()
    tx = repo.branch("teams/t").begin()
    tx.write("doc.md", base)
    tx.commit("base")
    repo.fork("teams/t", "agents/x")                 # both workers start from the team's version
    repo.fork("teams/t", "agents/y")
    tx = repo.branch("agents/x").begin()
    tx.write("doc.md", base.replace(b"line 2\n", b"LINE TWO\n"))
    tx.commit("x edits top")
    tx = repo.branch("agents/y").begin()
    tx.write("doc.md", base.replace(b"line 17\n", b"LINE SEVENTEEN\n"))
    tx.commit("y edits bottom")
    repo.merge("agents/x", "teams/t")
    repo.merge("agents/y", "teams/t")
    merged = repo.branch("teams/t").read("doc.md")
    assert b"LINE TWO" in merged and b"LINE SEVENTEEN" in merged


def test_real_conflicts_are_reported_and_resolvable(repo):
    for name, text in (("agents/x", b"x wins"), ("agents/y", b"y wins")):
        tx = repo.branch(name).begin()
        tx.write("same.bin", b"\x00base")
        tx.commit("base")
    repo.merge("agents/x", "teams/t")
    for name, text in (("agents/x", b"\x00x"), ("agents/y", b"\x00y")):
        tx = repo.branch(name).begin()
        tx.write("same.bin", text)
        tx.commit("edit")
    repo.merge("agents/x", "teams/t")
    with pytest.raises(MergeConflict) as e:
        repo.merge("agents/y", "teams/t")
    assert e.value.paths == ["same.bin"]
    repo.merge("agents/y", "teams/t", resolve={"same.bin": "theirs"})
    assert repo.branch("teams/t").read("same.bin") == b"\x00y"


def test_leases_and_fencing(repo):
    f1 = repo.lease("shared", "plan.md", holder="agent:a", ttl=60)
    assert f1 == 1
    assert repo.lease("shared", "plan.md", holder="agent:b", ttl=60) is None    # held
    repo.release("shared", "plan.md", holder="agent:a")
    f2 = repo.lease("shared", "plan.md", holder="agent:b", ttl=60)
    assert f2 == 2                                                               # fences only increase
