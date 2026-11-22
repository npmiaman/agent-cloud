"""Lineage, the reverse index, retraction and taint (SPEC §5, §8).

Builds a derivation graph across many agents and branches, with a known reference graph,
and checks that downstream queries and retractions are exact: before compaction (tail
only), after compaction (segments only), and mixed (segments plus new tail).
"""
import os
import random

import pytest

from agentcloud_core import MemoryStore, Repo


def _reference_downstream(edges, root):
    out, frontier = set(), [root]
    fwd = {}
    for a, b in edges:
        fwd.setdefault(a, set()).add(b)
    while frontier:
        nxt = []
        for n in frontier:
            for m in fwd.get(n, ()):
                if m not in out:
                    out.add(m)
                    nxt.append(m)
        frontier = nxt
    return out


def _build(repo, seed, agents=12, steps=10):
    rng = random.Random(seed)
    edges, all_provs = [], []
    for step in range(steps):
        for a in range(agents):
            b = repo.branch(f"agents/a{a}")
            tx = b.begin(writer=f"agent:a{a}")
            k = rng.randint(0, min(3, len(all_provs)))
            inputs = rng.sample(all_provs, k) if k else []
            p = tx.write(f"a{a}/s{step}.md", f"a{a} step {step} from {len(inputs)}".encode(), inputs=inputs)
            tx.commit(f"step {step}")
            edges += [(i, p) for i in inputs]
            all_provs.append(p)
        if step % 3 == 2:
            repo.merge_many([f"agents/a{a}" for a in range(agents)], "project")
    return edges, all_provs


@pytest.fixture
def repo():
    return Repo.init(MemoryStore(), "lin", key=os.urandom(32))


@pytest.mark.parametrize("seed", range(4))
def test_downstream_is_exact_before_and_after_compaction(repo, seed):
    edges, provs = _build(repo, seed)
    rng = random.Random(100 + seed)
    roots = rng.sample(provs[:40], 5)
    for r in roots:
        assert repo.downstream(r) == _reference_downstream(edges, r)          # tail only
    stats = repo.compact_index()
    assert stats["edges"] == len(edges)
    for r in roots:
        assert repo.downstream(r) == _reference_downstream(edges, r)          # segments only
    more_edges, more = _build(repo, seed + 50, agents=4, steps=3)              # new commits after compaction
    edges += more_edges
    for r in roots + more[:3]:
        assert repo.downstream(r) == _reference_downstream(edges, r)          # segments + tail


def test_retraction_flags_everything_downstream_and_future_derivations(repo):
    edges, provs = _build(repo, seed=7)
    root = provs[3]
    expected = _reference_downstream(edges, root)
    r = repo.retract(root, reason="source was fabricated", by="human:reviewer")
    assert r["affected"] == expected and len(expected) > 0
    assert repo.is_retracted(root) and all(repo.is_retracted(p) for p in expected)
    clean = next(p for p in provs if p != root and p not in expected)
    assert not repo.is_retracted(clean)
    tx = repo.branch("agents/late").begin(writer="agent:late")
    p = tx.write("late/derived.md", b"built on a retracted result", inputs=[next(iter(expected))])
    tx.commit("late")
    assert repo.prov(p)["trust"] == "untrusted"                              # flagged at write time
    assert repo.is_retracted(p)
    hits = repo.retracted_files("project")
    assert hits and all(repo.is_retracted(h["prov"]) for h in hits)


def test_trust_propagates_through_inputs(repo):
    tx = repo.branch("agents/web").begin(writer="agent:web")
    web = tx.write("web.md", b"page text", sources=["https://example.com"])
    tx.commit("web")
    tx = repo.branch("agents/sum").begin(writer="agent:sum")
    s = tx.write("summary.md", b"summary", inputs=[web])
    clean = tx.write("own.md", b"own idea")
    tx.commit("sum")
    assert repo.prov(web)["trust"] == "untrusted" and repo.prov(s)["trust"] == "untrusted"
    assert repo.prov(clean)["trust"] == "trusted"


def test_blast_radius_demo_scale(repo):
    """One poisoned result, thousands of derived files across many agents."""
    tx = repo.branch("agents/source").begin(writer="agent:source")
    poison = tx.write("data/poisoned.csv", b"bad numbers")
    tx.commit("p")
    layer = [poison]
    total = 0
    for depth in range(4):
        nxt = []
        for a in range(10):
            tx = repo.branch(f"agents/d{depth}a{a}").begin(writer=f"agent:d{depth}a{a}")
            for k in range(8 if depth < 3 else 30):
                nxt.append(tx.write(f"d{depth}/a{a}/f{k}.md", f"{depth}-{a}-{k}".encode(), inputs=[random.choice(layer)]))
            tx.commit("derive")
        total += len(nxt)
        layer = nxt
    r = repo.retract(poison, reason="poisoned dataset", by="human:sec")
    assert len(r["affected"]) == total == 80 + 80 + 80 + 300
