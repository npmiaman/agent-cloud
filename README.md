<div align="center">

# Agent Cloud

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
  workspace wsp_…  (1024 MB, read_write, approval: none)
  credential aca_…  (shown once)

  # Claude Code
  claude mcp add --transport http agent-cloud http://127.0.0.1:8787/mcp --header "Authorization: Bearer aca_…"

  # Cursor (~/.cursor/mcp.json) and most MCP clients
  { "mcpServers": { "agent-cloud": { "url": "http://127.0.0.1:8787/mcp", "headers": { "Authorization": "Bearer aca_…" } } } }
```

Your agent already knows who it is and what it can reach. It works with paths like
`/research/notes.md`, and its files survive restarts, model swaps and moves between
machines. Next: the [getting-started guide](docs/getting-started.md).

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

`python bench/bench.py` on a MacBook (8 cores): 20 agents, 1,000 files, real HTTP round
trips to a separate server process.

| Operation | p50 |
|---|---|
| Read a file | 0.89 ms |
| Write a 1 KB file (versioned, audited, signed provenance) | 1.6 ms |
| MCP `cloud_read` | 0.59 ms |
| List a 100-file folder | 1.8 ms |
| Content search across 1,000 files | 14 ms |
| **Mixed reads, 8 concurrent clients, 1 process** | **429 req/s** |
| **Mixed reads, 8 concurrent clients, 4 processes** (`--workers 4`) | **2,969 req/s** (measured on v0.4) |

What made the difference:
- Credential "last used" times are written at most once a minute.
- Read audit events are batched every 250 ms.
- SQLite runs in WAL mode with memory-mapped reads, and each permission check is one query.
- A fast JSON path: FastAPI's generic encoder took about a third of CPU.
- Search caches a per-file chunk index.
- Multi-process serving.

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
- [ ] File-change wake-ups and the MCP Tasks extension
- [ ] `subscriptions/listen` push notifications
- [ ] Postgres metadata and S3/R2/GCS blob backends (multi-host)
- [ ] OAuth for MCP
- [ ] Erasure by deleting per-subject encryption keys, with an erasure ledger that survives restores
- [ ] OpenTelemetry export
- [ ] v0.5 features in the TypeScript SDK
- [ ] Whole-agent export and import

## Status and limits

- **v0.9, single node.** SQLite metadata and local-disk blobs, with an optional replica and backups.
- **Rate limits are per process.**
- **Bearer tokens only** for now. Put TLS in front for anything beyond localhost.
- **Trust labels reduce prompt-injection risk; they don't eliminate it.** See the
  [trust docs](docs/trust-and-provenance.md#limits).

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
