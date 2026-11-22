# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may include
breaking changes; they are called out below.

## [Unreleased]

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

[Unreleased]: https://github.com/npmiaman/agent-cloud/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/npmiaman/agent-cloud/releases/tag/v0.5.0
