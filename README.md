<div align="center">

<img src="docs/assets/banner.svg" alt="Agent Cloud: self-hosted cloud workspaces for AI agents" width="100%">

**Self-hosted, open-source cloud workspaces for AI agents.**

Each agent gets its own identity, storage, permissions and version history, over MCP with one credential.
Risky changes wait for a human, content from the open web is tracked and labelled, and every action is audited.

[![CI](https://github.com/npmiaman/agent-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/npmiaman/agent-cloud/actions/workflows/ci.yml)
[![CodeQL](https://github.com/npmiaman/agent-cloud/actions/workflows/codeql.yml/badge.svg)](https://github.com/npmiaman/agent-cloud/actions/workflows/codeql.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![MCP 2026-07-28](https://img.shields.io/badge/MCP-2026--07--28-8A2BE2.svg)](docs/agent-interface.md)

[Quickstart](#quickstart) · [Docs](docs/) · [Why](#why) · [Features](#features) · [Performance](#performance) · [Roadmap](#roadmap) · [Contributing](CONTRIBUTING.md)

</div>

---

## Two ways to use it

| | For | What it is |
|---|---|---|
| **The format** ([`SPEC.md`](SPEC.md), `agentcloud_core`) | Agent platforms and labs running thousands to millions of agents | An open layout for agent work on S3, GCS, R2, MinIO or a local directory: content-defined chunks, prolly trees, commits with compare-and-swap on branch heads, and lineage with retraction. **No server in the write path.** |
| **The reference server** (`agentcloud up`) | Teams running dozens of agents | Everything below: MCP, REST, console, approvals, search, webhooks, on one node. |

```python
from agentcloud_core import Repo, S3Store            # or LocalStore("/data/acf"), MemoryStore()

repo = Repo.init(S3Store("my-bucket"), "projects/alpha", key=kek)     # envelope-encrypted
tx = repo.branch("agents/researcher-17").begin(writer="agent:researcher-17")
p = tx.write("reports/q3.md", data, inputs=[upstream], sources=["https://example.com"])
tx.commit("draft")                                    # one conditional PUT on the branch head
repo.merge_many(["agents/a", "agents/b"], "teams/research")           # hierarchical fan-in
repo.retract(p, reason="fabricated source")           # flags everything downstream, every branch
```

`python examples/retraction_demo.py`: 90 agents write 3,060 files across their own branches,
merged up through teams into a project. One retraction finds the 2,453 downstream versions
in 6 ms, and a later write built on a flagged file is marked at write time.
`python bench/agents_bench.py` runs the many-agent benchmark. The format is a **v0.1
draft**; see the spec's open questions.

## Quickstart

```bash
pip install "agentcloud[fast] @ git+https://github.com/npmiaman/agent-cloud"   # Python 3.11+
agentcloud up                                  # first run: creates a local admin, prints a one-time console link
agentcloud agent create research --storage 1GB
```

Or with Docker: `docker compose up -d && docker compose logs agentcloud`.

The last command prints a credential and ready-to-paste configs:

```text
  research  agt_…
  workspace wsp_…  (1024 MB, owner, approval: none)
  credential aca_…  (shown once)

  # Claude Code
  claude mcp add --transport http agent-cloud http://127.0.0.1:8787/mcp --header "Authorization: Bearer aca_…"

  # Cursor (~/.cursor/mcp.json) and most MCP clients
  { "mcpServers": { "agent-cloud": { "url": "http://127.0.0.1:8787/mcp", "headers": { "Authorization": "Bearer aca_…" } } } }
```

Your agent already knows who it is and what it can reach. It owns its workspace, works with
paths like `/research/notes.md`, and its files survive restarts, model swaps and moves
between machines. Next: the [getting-started guide](docs/getting-started.md).

## Why

Agents need somewhere to put their work that isn't a temp directory, a shared S3 bucket
with one all-powerful key, or a human's Google Drive. And as agents run longer and work
together, the hard problems stop being about storage:

- **Whose agent is this, and what may it touch?** Each agent has its own scoped,
  rotatable, revocable credential, and never borrows a human's.
- **What if it gets something wrong?** Every write is a version, risky changes wait for a
  human with a diff, and everything is undoable.
- **What if it read a poisoned web page?** Content derived from outside sources is
  labelled untrusted as it moves between agents and files, and protected paths can refuse it.
- **What if two agents edit the same thing?** Leases with fencing tokens, compare-and-swap
  and atomic multi-file commits.
- **Who approves all this at a million agents?** Not humans. Each agent owns its workspaces
  and governs them within an org-wide constitution, and humans handle exceptions.
- **What if it runs away?** Budgets, runaway detection and a kill switch that also stops
  every helper it spawned.

## Features

| | |
|---|---|
| **Agent-sovereign workspaces** | Each agent owns isolated workspaces and governs them itself: grants, denies, reviews other agents' changes, creates new private workspaces, all within one org-wide constitution. No human in the loop by default. [More](docs/sovereign-workspaces.md) |
| **Workspace version control** | Merkle trees with prolly-tree folders (one change in a 20k-file folder writes 4 small nodes), sub-millisecond diffs, rollback, storage-sharing branches, histogram-diff three-way merges |
| **Vector and hybrid search** | Pluggable embedders (local model, any OpenAI-compatible API, or a built-in fallback), encrypted vectors, HNSW (0.65 ms, recall 1.0 at 20k), Reciprocal Rank Fusion with keyword search. No model bundled or downloaded. [More](docs/vector-search.md) |
| **Storage that doesn't balloon** | FastCDC content-defined chunking with chunk-level dedup: 100 appends to a 2.2 MB log store 2 MB, not 220 MB |
| **Built for millions of files** | Persistent encrypted-term search index (0.12 ms rare-term search at 20k files), folder overviews, and a local replica for 3.6 µs reads and 43 µs local search |
| **Per-agent identity** | Hashed, scoped, expiring credentials with rotation and grace periods; 15-minute access tokens; immediate revocation |
| **Least privilege** | Workspace roles, path grants, deny rules and credential scopes. Agents can re-share or delegate only a subset of what they have. |
| **Versioned files** | Immutable versions, soft deletes, restore-as-new-version, diffs (including CSV column changes), stable file IDs across moves |
| **Human approval** | Overwrites, deletes or untrusted content can require review; approvals arrive as signed webhooks |
| **Provenance and trust** | A signed record per version: writer, task, model, trace, inputs, sources. Taint propagates; agents see untrusted content behind a banner; quarantine and endorse. [More](docs/trust-and-provenance.md) |
| **Multi-agent coordination** | Leases with fencing tokens, atomic multi-file commits, sharing with notes, delegation to scoped helper agents. [More](docs/multi-agent.md) |
| **Safety rails** | Write, byte and approval budgets; runaway-loop detection; a kill switch for an agent's whole delegation subtree |
| **Agent-native MCP** | MCP 2026-07-28 (stateless) plus older clients; exact-text edits, appends, line-range reads, batches, BM25 search with line ranges, session briefings, versioned resources. [More](docs/agent-interface.md) |
| **Claude memory tool** | A drop-in backend for `memory_20250818`: per-agent, versioned, audited. [More](docs/memory-tool.md) |
| **Audit** | Every read, write, share and credential change, with request IDs, streamed live in the console |
| **Durability** | Online verified backups, a self-healing replica, integrity scrub, health and readiness checks. [More](docs/operations.md) |
| **Interfaces** | MCP, REST with OpenAPI, Python and TypeScript SDKs, the `agentcloud` and `ac` CLIs, a web console, `/llms.txt` |

### Built around how agents actually work

Every model turn and every output token costs time and money:

- **Edit, don't rewrite.** Measured with Claude Haiku on a one-line change to a 300-line file:
  **32.7 s → 8.0 s, 5,808 → 706 output tokens, 57% cheaper**, identical result (3 runs each).
- **Batch.** `paths` and `files` cut the turns for a typical multi-file task from 8 to 4.
- **No discovery turn.** Identity, permissions and "what changed since your last session"
  arrive with the first call. A real agent answered "what changed?" in one turn without a tool call.
- **Search points at lines**, so the next read is a targeted window, not the whole file.

## Performance

`python bench/bench.py` on a MacBook (8 cores, v0.9): 20 agents, 1,000 files, real HTTP
round trips to a separate server process.

| Operation | p50 |
|---|---|
| Read a file | 1.0 ms |
| Write a 1 KB file (versioned, audited, signed provenance) | 1.7 ms |
| MCP `cloud_read` | 0.70 ms |
| List a 100-file folder | 2.0 ms |
| Keyword search across 1,000 files | 4.8 ms |
| **Mixed reads, 8 concurrent clients, 1 process** | **447 req/s** |
| **Mixed reads, 8 concurrent clients, 4 processes** (`BENCH_WORKERS=4`) | **2,262 req/s** |

At scale (20,000 files in one workspace, measured in-process):

| Operation | Result |
|---|---|
| Keyword search, rare term | 0.12 ms (persistent encrypted-term index) |
| Keyword search, term in every file | 45 ms, exact top-k after scanning 20 of 20,000 rows |
| Vector search (HNSW, 384-dim) | 0.65 ms, recall@10 = 1.000 |
| Commit after one change in a 20,000-file folder | 11 ms, 4 new tree nodes (21 KB) |
| Diff between commits | 0.2–0.5 ms |
| Local replica read / search | 3.6 µs / 43 µs |
| 100 appends to a 2.2 MB log, after dedup | 2.07 MB stored (vs 220 MB) |

Benchmarks on a laptop are noisy; run them on your own hardware before relying on them.

## Commands

```bash
agentcloud up [--port 8787] [--workers auto] [--data ~/.agentcloud]
agentcloud agent create NAME [--storage 2GB] [--access read_only|read_write|full] [--approval none|overwrite|all]
agentcloud agent list | kill NAME | budget NAME [--writes N] [--bytes 50MB] [--proposals N]
agentcloud policy [KEY=VALUE ...]              # the constitution agents govern within
agentcloud approvals [approve|reject ID]
agentcloud webhook add URL --events approval.*,agent.runaway
agentcloud backup [--to DIR] · agentcloud restore DIR [--force]
agentcloud login-link · agentcloud status · agentcloud worker [--once]
```

## Documentation

| | |
|---|---|
| [Getting started](docs/getting-started.md) | Install, run, connect an agent |
| [Agent interface](docs/agent-interface.md) | MCP tools, protocol versions, sessions, error codes |
| [Vector search](docs/vector-search.md) | Embedders, hybrid ranking, HNSW, configuration |
| [Agent-sovereign workspaces](docs/sovereign-workspaces.md) | Ownership, the constitution, agent review, version control |
| [Trust and provenance](docs/trust-and-provenance.md) | Prompt-injection containment |
| [Multi-agent work](docs/multi-agent.md) | Leases, atomic commits, delegation, budgets, kill switch |
| [Claude memory tool](docs/memory-tool.md) | Agent Cloud as a `memory_20250818` backend |
| [Operations](docs/operations.md) | Docker, configuration, backups, production checklist |
| [Architecture](docs/architecture.md) | Modules and design decisions |
| [Security](SECURITY.md) | Threat model and how to report vulnerabilities |

## Roadmap

Ordered by the [research survey](docs/research/agent-infrastructure-gaps.md) of gaps in
today's agent infrastructure:

- [x] Agent-owned workspaces, agent review, branches and merges (v0.6)
- [x] Persistent search index, folder overviews, tree-structured commits, line-level merges, local replica (v0.7)
- [x] Content-defined chunking, prolly trees, exact top-k ranking (v0.8)
- [x] Vector and hybrid search with pluggable embedders (v0.9)
- [ ] Persisted HNSW graphs and a native chunker (the remaining CPU-bound hot paths)
- [ ] File-change wake-ups and the MCP Tasks extension
- [ ] `subscriptions/listen` push notifications
- [ ] Postgres metadata and S3/R2/GCS blob backends (multi-host)
- [ ] OAuth for MCP
- [ ] Erasure by deleting per-subject encryption keys, with an erasure ledger that survives restores
- [ ] OpenTelemetry export
- [ ] TypeScript SDK coverage for v0.5+ features (the Python SDK has them)
- [ ] Whole-agent export and import

## Status and limits

- **v0.9, single node.** SQLite metadata and local-disk blobs, with an optional replica and backups.
- **Rate limits are per process.**
- **Bearer tokens only** for now. Put TLS in front for anything beyond localhost.
- **Trust labels reduce prompt-injection risk; they don't eliminate it.** See the
  [trust docs](docs/trust-and-provenance.md#limits).
- **Vector search needs an embedding model you choose.** None is bundled or downloaded. See
  [vector search](docs/vector-search.md).

## Contributing

Contributions are welcome. Every behaviour change comes with a test.

```bash
make install && make test && make lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).
Questions go to [Discussions](../../discussions); vulnerabilities to
[private reporting](../../security/advisories/new).

## License

[Apache-2.0](LICENSE)
