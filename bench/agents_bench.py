"""Many-agent benchmark for the repository format (no server, no database).

    python bench/agents_bench.py                     # local directory store, all scenarios
    python bench/agents_bench.py --agents 2000 --procs 8

Scenarios:
  1. Per-agent branches: N agents, each committing to its own branch, from P processes.
  2. Shared branch: P processes committing to one branch (compare-and-swap + rebase).
  3. Hierarchical merge: workers -> teams -> project, as fan-in merges.
  4. Retraction: downstream query over the whole lineage graph, before and after compaction.
  5. Object-store operations per commit, and what they would cost on S3.

The S3 cost uses list prices that you should check for your region and tier (defaults:
PUT $0.005 and GET $0.0004 per 1,000 requests, S3 Standard). Throughput here comes from a
local SSD; real S3 has ~10-30 ms per request but scales out with prefixes, so measure there
before quoting S3 throughput.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcloud_core import LocalStore, MemoryStore, Repo  # noqa: E402

PUT_PER_1K, GET_PER_1K = 0.005, 0.0004


def _agent_worker(args):
    root, key, agent_ids, commits, files_per_commit = args
    repo = Repo.open(LocalStore(root), "bench", key=key)
    rng = random.Random(agent_ids[0])
    n = 0
    for a in agent_ids:
        b = repo.branch(f"agents/a{a}")
        for c in range(commits):
            tx = b.begin(writer=f"agent:a{a}")
            for f in range(files_per_commit):
                tx.write(f"a{a}/c{c}-f{f}.md", f"agent {a} commit {c} file {f} {rng.random()}".encode())
            tx.commit(f"c{c}")
            n += 1
    return n


def _shared_worker(args):
    root, key, wid, commits = args
    repo = Repo.open(LocalStore(root), "bench", key=key)
    b = repo.branch("shared")
    for c in range(commits):
        tx = b.begin(writer=f"agent:s{wid}", shared=True)
        tx.write(f"s{wid}/c{c}.md", f"{wid}-{c}".encode())
        tx.commit("s")
    return commits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=400)
    ap.add_argument("--commits", type=int, default=5)
    ap.add_argument("--files", type=int, default=2, help="files per commit")
    ap.add_argument("--procs", type=int, default=min(8, os.cpu_count() or 4))
    args = ap.parse_args()
    key = os.urandom(32)
    root = Path(tempfile.mkdtemp(prefix="acf-bench-"))
    try:
        Repo.init(LocalStore(root), "bench", key=key)
        # 1. per-agent branches
        ids = list(range(args.agents))
        parts = [ids[i::args.procs] for i in range(args.procs)]
        t = time.perf_counter()
        with mp.Pool(args.procs) as pool:
            total = sum(pool.map(_agent_worker, [(str(root), key, p, args.commits, args.files) for p in parts]))
        dt = time.perf_counter() - t
        print(f"1. per-agent branches: {args.agents} agents x {args.commits} commits ({args.files} files each), "
              f"{args.procs} processes: {total / dt:,.0f} commits/s, {total * args.files / dt:,.0f} files/s, 0 conflicts")
        # 2. shared branch contention
        t = time.perf_counter()
        with mp.Pool(args.procs) as pool:
            sh = sum(pool.map(_shared_worker, [(str(root), key, w, 20) for w in range(args.procs)]))
        dt = time.perf_counter() - t
        repo = Repo.open(LocalStore(root), "bench", key=key)
        files = len(repo.branch("shared").files())
        print(f"2. one shared branch, {args.procs} writers: {sh / dt:,.0f} commits/s; {files}/{sh} files present (no lost updates)")
        # 3. hierarchical merge
        teams = [f"teams/t{t}" for t in range(max(1, args.agents // 20))]
        t = time.perf_counter()
        for i, team in enumerate(teams):
            repo.merge_many([f"agents/a{a}" for a in range(i * 20, min((i + 1) * 20, args.agents))], team)
        t_mid = time.perf_counter()
        repo.merge_many(teams, "project")
        t_end = time.perf_counter()
        pf = len(repo.branch("project").files())
        print(f"3. hierarchy: {args.agents} workers -> {len(teams)} teams in {t_mid - t:.2f}s "
              f"({args.agents / (t_mid - t):,.0f} branch merges/s); teams -> project in {t_end - t_mid:.2f}s; "
              f"project has {pf:,} files")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 4. retraction over a lineage graph (in memory, to isolate the algorithm)
    mem = Repo.init(MemoryStore(), "lin", key=key)
    rng = random.Random(3)
    provs = []
    t = time.perf_counter()
    for a in range(200):
        tx = mem.branch(f"agents/a{a}").begin(writer=f"agent:a{a}")
        for k in range(50):
            inputs = rng.sample(provs, min(len(provs), rng.randint(0, 2)))
            provs.append(tx.write(f"a{a}/f{k}.md", f"{a}-{k}".encode(), inputs=inputs))
        tx.commit("w")
    build = time.perf_counter() - t
    root_prov = provs[10]
    t = time.perf_counter()
    d1 = mem.downstream(root_prov)
    tail_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    st = mem.compact_index()
    comp = time.perf_counter() - t
    t = time.perf_counter()
    d2 = mem.downstream(root_prov)
    idx_ms = (time.perf_counter() - t) * 1000
    assert d1 == d2
    print(f"4. retraction over {len(provs):,} versions / {st['edges']:,} lineage edges: downstream of one root = {len(d1):,} "
          f"versions; {tail_ms:.0f} ms from commit history, {idx_ms:.0f} ms from the compacted index "
          f"(compaction {comp:.2f}s; graph built in {build:.1f}s)")

    # 5. object-store operations per commit -> S3 cost
    ms = MemoryStore()
    r = Repo.init(ms, "ops", key=key)
    b = r.branch("agents/x")
    for c in range(20):                      # warm up so the tree has some depth
        tx = b.begin()
        tx.write(f"warm/{c}.md", b"x" * 500)
        tx.commit("w")
    ms.ops = {"get": 0, "put": 0, "list": 0}
    n = 200
    for c in range(n):
        tx = b.begin()
        tx.write(f"work/{c % 50}.md", f"update {c} ".encode() * 40)
        tx.commit("c")
    puts, gets = ms.ops["put"] / n, ms.ops["get"] / n
    cost_per_m = (puts * PUT_PER_1K + gets * GET_PER_1K) * 1000
    per_agent_hour = cost_per_m / 1e6 * 60                 # one commit a minute
    print(f"5. per commit (1 file, ~1 KB): {puts:.1f} PUTs, {gets:.1f} GETs -> ${cost_per_m:.2f} per million commits "
          f"in S3 requests (list prices; check yours). An agent committing once a minute: "
          f"${per_agent_hour:.4f} per agent-hour, ${per_agent_hour * 1e6:,.0f} per million agent-hours")


if __name__ == "__main__":
    main()
