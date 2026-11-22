# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may include
breaking changes; they are called out below.

## [Unreleased]

### Added
- **`SPEC.md`:** the Agent Cloud repository format v0.1 (draft). Object layout,
  content-defined chunks, prolly trees, the compare-and-swap commit protocol, hierarchical
  merges, lineage with an LSM-style reverse index, retraction, leases, encryption, GC.
- **`agentcloud_core`:** the reference library, with no server and no database.
  - Backends: `MemoryStore`, `LocalStore`, `S3Store` (conditional PUTs).
  - Envelope encryption with keyed object ids; branches; shared-branch rebase and retry;
    fan-in merges with diff3; downstream queries; retraction; leases.
  - Conformance tests run against every backend, S3 through moto.
- `examples/retraction_demo.py` (blast-radius demo) and `bench/agents_bench.py` (many-agent benchmark with S3 request costs).
- Optional extra `s3` (boto3).

### Changed
- `cdc`, `diffalgo` and `merge3` moved into `agentcloud_core`; the server re-exports them.

## [0.9.0] - 2026-11-23

Vector search, without shipping or downloading a model.

### Added
- **Pluggable embedders** (`AGENTCLOUD_EMBEDDER`):
  - built-in `hashing` (lexical, no download);
  - `sentence-transformers:<model>`, local-cache only unless `AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD=1`, imported lazily;
  - `http:<url>|<model>` for any OpenAI-compatible endpoint.
- **HNSW index** with the diversity heuristic, tombstone deletes with automatic rebuilds,
  and filtered search with an exact fallback. Uses numpy if present, pure Python otherwise.
  0.65 ms p50 and recall@10 = 1.000 at 20,000 × 384-dim.
- **Encrypted vector store** (AES-GCM), tagged by model, with background and on-search
  embedding queues and a change log so every process stays current.
- **Hybrid search** with Reciprocal Rank Fusion: `cloud_search(mode=auto|keyword|semantic|hybrid)`, `GET /v1/search?mode=`.
- `GET /v1/vector/status`; optional extras `vector` (numpy) and `local-embeddings`.

### Changed
- Database triggers are recreated on start, so upgrades always run the current definitions.

## [0.8.0] - 2026-11-23

Algorithms. Each change is test-first and measured at 20,000 files.

### Added
- **FastCDC content-defined chunking with chunk-level dedup.**
  - Gear rolling hash, normalised chunking, 2/8/64 KiB min/avg/max.
  - Writes stay fast (files are stored whole); the worker's dedup pass rewrites big files as
    encrypted manifests over shared, encrypted chunks.
  - 100 appends to a 2.2 MB log: **220 MB → 2.07 MB** stored.
  - Chunk-aware reads (healing from the replica), scrub, repair, backups and mark-and-sweep GC.
- **Prolly trees for folders.**
  - Content-defined node boundaries on entry names, so the same entries always give the same hash, however they were built.
  - One change in a 20,000-file folder writes **4 nodes (21 KB)** instead of a ~2 MB folder object.
  - Diffs drop shared nodes level by level (0.47 ms).
  - Folders under 32 entries keep the original single-object format.
- **Exact top-k search via the Threshold Algorithm.**
  - FTS5 BM25 with column weights, a term-proximity factor (smallest window covering the matched terms), and the coverage rule pushed into the FTS query.
  - Results are identical to an exhaustive scan. A term present in every file examined 20 rows instead of 20,000 (45 ms vs 5.75 s).
  - Scores are normalised to the top hit.
- **Histogram diff** (git's algorithm) under the three-way merge.

### Fixed
- The dedup pass could be starved by small files and stop early.

### Notes
- Over 20,000 random synthetic merges, histogram diff and difflib were statistically tied (323 vs 335 clean merges where the other conflicted); histogram is kept for git-compatible alignment, not for a measured win on this data.
- Chunking runs at ~9 MB/s in pure Python, so dedup is a background pass. This is the clearest candidate for a native (Rust) extension.

## [0.7.0] - 2026-11-23

Scale: millions of files per agent. Every number below was measured with 20,000 files in one workspace.

### Added
- **Persistent search index (FTS5).**
  - Writes enqueue; searches index anything queued for their workspaces first, so results include your latest writes.
  - Terms are stored as keyed hashes, never plaintext, so contents stay encrypted at rest.
  - Same BM25 ranking, 60% coverage rule and name bonus as before; only the top hits are decrypted, for snippets.
  - Rare-term search: **0.12 ms** (was a full scan).
  - Falls back to the scan if SQLite lacks FTS5.
- **`cloud_overview`:** per-folder file counts, bytes and last change down to N levels, plus recent files.
- **Tree-structured commits (Merkle trees).**
  - Commits store content-addressed folder trees, and unchanged folders are shared.
  - Commits after the first are incremental (**2.1 ms**).
  - Diffs and rollbacks skip identical subtrees (**0.2 ms** for a diff between commits).
  - Checked against the sequence-based ground truth on 68 random operation histories.
- **Line-level three-way merge (diff3).** Edits to different lines of the same text file
  merge automatically. Real conflicts return hunks and conflict-marked text, and can be
  resolved with `{"content": ...}`.
- **Local replica** (`agentcloud.replica.LocalReplica`) and sync feed
  (`GET /v1/workspaces/{id}/sync`, `POST /v1/workspaces/{id}/sync/content`).
  - A workspace mirrored into local SQLite: reads in **3.6 µs** (0.2 µs cached), local search in **43 µs**.
  - Incremental sync fetches only changes (**4.3 ms** for one change).
  - Revoked access is removed from the replica.
  - Local reads are declared as inputs on write (`X-Agent-Inputs`), so trust labels still propagate.

### Changed
- Very common search terms: only the 300 best FTS candidates are fully scored, a bounded
  approximation of the old exhaustive ranking (31 ms at 20,000 files).
- Writes are slightly faster (benchmark p50 1.6 ms → 1.4 ms).

## [0.6.0] - 2026-11-23

Agent-sovereign workspaces: agents govern their own isolated workspaces, so humans aren't the bottleneck.

### Added
- **Owner agents.** `agentcloud agent create` now makes the agent the owner of its home workspace by default.
- **`cloud_permissions`:** owner agents grant, deny and revoke access to their workspaces,
  optionally expiring or with `approval=all` so they review the grantee's changes.
- **`cloud_review`:** owner agents approve or reject other agents' proposals, with diffs.
  No self-approval.
- **`cloud_workspace`:** owner agents create new private workspaces carved from their own
  quota, and set trust policies.
- **Organisation constitution** (`agentcloud policy`, `/v1/organisation/agent-policy`):
  what agents may grant, for how long, and to whom. Safe defaults.
- **Untrusted context can't widen access:** an agent that recently read untrusted content
  gets `403 UNTRUSTED_CONTEXT` when granting.
- **Version control.** `cloud_commit` (O(1) commits, log, diff, rollback) and
  `cloud_branch` (storage-sharing branches, preview, three-way merge with per-file
  conflict resolution).
- Path history for moves, so snapshots see files at their historical paths.

### Fixed
- **Writes no longer slow down as a workspace grows.** Quota and dedup checks scanned every
  version on every write. They now use a trigger-maintained per-workspace content table and
  byte counter, backfilled once on upgrade. Writing 20,000 files into one workspace: 328 s → 54 s.

### Changed
- Agents that can see a workspace get `403` (not `404`) when they try to review its proposals without owning it.
- `cloud_share` still works when `agents_govern` is off: agents can always share a subset of their own access.

## [0.5.0] - 2026-11-23

Long-running and multi-agent work.

### Added
- **MCP 2026-07-28.** Stateless requests with per-request `_meta`, `server/discover`,
  `resultType`, `ttlMs`/`cacheScope`, and `UnsupportedProtocolVersionError`. Older
  `initialize`-based clients keep working on the same endpoint.
- **Session briefing without `initialize`.** The first tool call after 30 quiet minutes
  carries the agent's identity, permissions and what changed since its last session.
- **Provenance.** A signed record per file version: writer, credential, task, model,
  client, `traceparent`, read set and cited sources. New `cloud_provenance` tool and
  `GET /v1/files/{id}/provenance`.
- **Trust labels.**
  - Content from outside sources, from `ingests_untrusted` agents, or derived from untrusted files is labelled untrusted, and agents see it behind a banner.
  - Workspace `trust_policy` can block untrusted writes to protected paths, or send them for approval.
  - Humans can quarantine a version (spreads to derived versions) or endorse one.
- **Leases** (`cloud_lease`) with fencing tokens; `423 LEASE_HELD`, `409 STALE_FENCE`.
- **Atomic multi-file commits:** `cloud_write(files=[...], atomic=true)` with per-file `if_version`.
- **Delegation** (`cloud_delegate`): helper agents with a subset of the parent's
  access, an expiry, and a budget clamped to what the parent has left.
- **Kill switch:** `POST /v1/agents/{id}/kill` and `agentcloud agent kill NAME` disable
  an agent and its whole delegation subtree.
- **Budgets** on writes, bytes and approval requests (`agentcloud agent budget`), plus
  runaway-loop detection with an `agent.runaway` webhook.
- **Claude memory tool backend:** `POST /v1/memory` for `memory_20250818` commands, and
  `agentcloud.memory.AgentCloudMemoryTool` for the anthropic SDK tool runner.
- REST writes accept `X-Agent-Task`, `X-Agent-Model` and `traceparent` for provenance.
- Dockerfile, CodeQL, Dependabot, issue and PR templates, docs site content.

### Changed
- Content search caches a per-file chunk index (benchmark p50 23.6 ms → 14 ms).
- Writes record provenance (benchmark p50 1.3 ms → 1.6 ms).
- MCP resources accept `?version=N` for immutable, cacheable reads.

## [0.4.0] - 2026-11-22

### Added
- Agent-native editing: `cloud_edit`, `cloud_append`, line-range reads, batched reads and writes.
- Identity and "since your last session" briefing in the MCP handshake; `cloud_changes`,
  `cloud_proposals`, `cloud_diff`; files as MCP resources; BM25 search with line ranges; `/llms.txt`.
- Durability: `agentcloud backup` / `restore` (online, verified), `AGENTCLOUD_REPLICA_DIR`
  self-healing replica, integrity scrub, `/readyz`, systemd and launchd units.

## [0.3.0] - 2026-11-22

### Added
- `agentcloud` operator CLI with zero-config `agentcloud up`, multi-process serving,
  signed webhooks with retries, developer console, benchmarks, Apache-2.0 licence.

## [0.2.0]

### Added
- Organisations and projects, data plane with upload sessions and signed URLs,
  idempotency keys, rate limits, cursor pagination, deny rules, TypeScript SDK.

## [0.1.0]

### Added
- First version: per-agent credentials, versioned encrypted workspaces, grants,
  approvals with diffs, audit log, MCP server, Python SDK.

[Unreleased]: https://github.com/npmiaman/agent-cloud/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/npmiaman/agent-cloud/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/npmiaman/agent-cloud/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/npmiaman/agent-cloud/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/npmiaman/agent-cloud/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/npmiaman/agent-cloud/releases/tag/v0.5.0
