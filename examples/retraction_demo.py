"""Blast-radius demo: one poisoned result, thousands of derived files, one retraction.

    python examples/retraction_demo.py                 # in memory
    python examples/retraction_demo.py --dir /tmp/acf  # on a local directory (same layout as S3)

A source agent publishes a dataset. Three layers of agents (each on its own branch) build
on it and on each other's outputs. Their work is merged up a worker -> team -> project
hierarchy. Then the dataset turns out to be poisoned: one retract() call finds every
downstream version across every agent and branch, and anything written later on top of it
is flagged at write time.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcloud_core import LocalStore, MemoryStore, Repo  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="store the repository in this directory instead of memory")
    ap.add_argument("--agents", type=int, default=30, help="agents per layer")
    ap.add_argument("--files", type=int, default=34, help="files each agent writes")
    args = ap.parse_args()
    store = LocalStore(args.dir) if args.dir else MemoryStore()
    repo = Repo.init(store, f"demo-{int(time.time())}", key=os.urandom(32))
    rng = random.Random(1)

    tx = repo.branch("agents/source").begin(writer="agent:source")
    poison = tx.write("data/prices.csv", b"date,price\n2026-01-01,999999\n")
    clean = tx.write("data/weather.csv", b"date,temp\n2026-01-01,12\n")
    tx.commit("publish datasets")

    t0 = time.perf_counter()
    upstream, total = {"poison": [poison], "clean": [clean]}, 0
    for layer in range(3):
        nxt = {"poison": [], "clean": []}
        for a in range(args.agents):
            name = f"agents/l{layer}-a{a:02d}"
            tx = repo.branch(name).begin(writer=f"agent:l{layer}-a{a:02d}")
            for k in range(args.files):
                lineage = "poison" if rng.random() < 0.8 else "clean"
                inputs = rng.sample(upstream[lineage], min(2, len(upstream[lineage])))
                p = tx.write(f"l{layer}/a{a:02d}/report-{k}.md", f"layer {layer} agent {a} file {k}".encode(), inputs=inputs)
                nxt[lineage].append(p)
                total += 1
            tx.commit(f"layer {layer} work")
        for team in range(0, args.agents, 10):
            repo.merge_many([f"agents/l{layer}-a{a:02d}" for a in range(team, min(team + 10, args.agents))],
                            f"teams/l{layer}-t{team // 10}")
        upstream = nxt
    teams = [b for b in repo.branches() if b.startswith("teams/")]
    repo.merge_many(teams + ["agents/source"], "project")
    build = time.perf_counter() - t0
    print(f"{3 * args.agents} agents wrote {total:,} files on their own branches, merged through "
          f"{len(teams)} team branches into 'project' in {build:.1f}s")

    t0 = time.perf_counter()
    r = repo.retract(poison, reason="price feed was poisoned", by="human:security")
    took = time.perf_counter() - t0
    flagged = repo.retracted_files("project")
    print(f"retract(data/prices.csv): {len(r['affected']):,} downstream versions found in {took * 1000:.0f} ms")
    print(f"files in 'project' now flagged as retracted: {len(flagged):,} of {len(repo.branch('project').files()):,}")

    tx = repo.branch("agents/late").begin(writer="agent:late")
    late = tx.write("late/summary.md", b"built on a flagged report", inputs=[next(iter(r["affected"]))])
    tx.commit("late")
    print(f"a later write built on a flagged report: trust={repo.prov(late)['trust']}, retracted={repo.is_retracted(late)}")


if __name__ == "__main__":
    main()
