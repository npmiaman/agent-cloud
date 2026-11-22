# Agent interface

Agents connect over MCP Streamable HTTP at `POST /mcp` with `Authorization: Bearer <credential>`.
Stdio-only clients use the bridge: `python -m agentcloud.mcp_stdio` (reads `AGENTCLOUD_URL`
and `AGENTCLOUD_TOKEN`).

## Protocol versions

| Client | How it works |
|---|---|
| **2026-07-28** (stateless) | No `initialize`. Send `io.modelcontextprotocol/protocolVersion` (and optionally `clientInfo`) in each request's `_meta`, or the `MCP-Protocol-Version` header. `server/discover` returns versions, capabilities and the agent-specific instructions. Results carry `resultType` and `_meta.io.modelcontextprotocol/serverInfo`. |
| **2025-11-25 and earlier** | The `initialize` handshake. Its `instructions` contain the agent's identity, permissions and briefing. |

An unsupported version gets error `-32022` with the supported list.

### Sessions and the briefing

Stateless clients never call `initialize`, so a **session starts with the first call
after 30 quiet minutes**. That call's result gets an extra `[New session]` block with:

- who the agent is, its workspaces, permissions and budget
- what changed since its previous session: other actors' edits to files it can read,
  decisions on its approval requests (with the reviewer's note), and access shared with it
  (with the sharer's note)

Agents can ask again at any time with `cloud_changes`.

### Optional `_meta` keys

Recorded in [provenance](trust-and-provenance.md):

| Key | Meaning |
|---|---|
| `agentcloud/task` | The task this call belongs to. Also scopes the read set (see trust docs). |
| `agentcloud/model` | The model making the call. |
| `traceparent` | W3C trace context, so you can join the record with your OpenTelemetry traces. |

## Tools

An agent is only offered tools it could succeed with somewhere: no `cloud_delete` without
delete rights, no `cloud_delegate` without share rights.

| Tool | What it does |
|---|---|
| `cloud_whoami` | Identity, workspaces, storage, permissions, budget, leases held |
| `cloud_list` | List a folder (`recursive` optional); untrusted files are marked |
| `cloud_read` | One file (`path`) or several (`paths`); `offset`/`limit`/`line_numbers` for a line window; `version` for an old one. Untrusted content comes with a warning banner. |
| `cloud_write` | One file or a batch (`files`, each with optional `if_version`). `atomic=true` commits all or none. `sources` marks content from outside material. `fence` passes a lease token. |
| `cloud_edit` | Exact-text replacements, all or nothing; much cheaper than rewriting a file |
| `cloud_append` | Append to a file (creates it); retries once on a concurrent write |
| `cloud_search` | BM25-ranked name and content search across workspaces; each hit says which `lines` matched |
| `cloud_mkdir` | Create a folder |
| `cloud_history` / `cloud_diff` / `cloud_restore` | Version history, unified diffs (with CSV column changes), restore as a new version |
| `cloud_changes` | What changed since the last session, or the last `hours` |
| `cloud_proposals` | The agent's approval requests and their outcomes |
| `cloud_share` | Give another agent read or read-write access to a path, with an optional note and expiry |
| `cloud_provenance` | Where a version came from and what was built from it |
| `cloud_lease` | Claim a file or folder; returns a fencing token |
| `cloud_delegate` | Create, list or revoke helper agents |
| `cloud_commit` | Workspace commits: create, log, diff, rollback |
| `cloud_permissions` / `cloud_review` / `cloud_workspace` / `cloud_branch` | For workspaces the agent owns; see [sovereign workspaces](sovereign-workspaces.md) |
| `cloud_move` / `cloud_delete` | Move or rename (history kept); soft delete (recoverable) |

Tool failures come back as tool results with `isError: true` and a structured `error`
code, so the model can recover. If a change needs approval, the result says so; the
original is unchanged until a human decides.

## Resources

Every file is an MCP resource: `agentcloud://<workspace>/<path>`. Add `?version=N` for a
fixed version; those responses carry a long `ttlMs`, since versions never change.

## Error codes worth handling

| Code | Status | Meaning |
|---|---|---|
| `VERSION_CONFLICT` | 409 | `if_version` didn't match; re-read and retry |
| `LEASE_HELD` | 423 | Another agent holds a lease; the result says who and for how long |
| `STALE_FENCE` | 409 | Your lease expired or was released; re-acquire and re-read |
| `BUDGET_EXCEEDED` | 429 | A write, byte or approval budget is used up |
| `RUNAWAY_SUSPECTED` | 429 | Repeated rewrites of one file; see [multi-agent](multi-agent.md#runaway-detection) |
| `UNTRUSTED_CONTENT` | 403 | A protected path refused content derived from untrusted input |
| `ATOMIC_NEEDS_APPROVAL` | 409 | An atomic batch included a file that needs approval; nothing was written |

## REST equivalents

Everything is also available over REST (OpenAPI at `/docs`). Agent-relevant endpoints:
`/v1/environment`, `/v1/search`, `/v1/changes`, `/v1/workspaces/{ws}/files/{path}` (GET/PUT/DELETE
with `If-Match`), `/edit/{path}`, `/append/{path}`, upload sessions with signed URLs, and
`/v1/memory`. REST writes accept `X-Agent-Task`, `X-Agent-Model` and `traceparent` headers.

## Local replica (microsecond reads)

Remote reads can't beat a network round trip. For agents that read their own work
constantly, mirror the workspace locally:

```python
from agentcloud.replica import LocalReplica
r = LocalReplica(workspace="wsp_…", token="aca_…", base_url="http://127.0.0.1:8787")
r.sync()                              # full copy first, then only changes
r.read_text("/notes/plan.md")         # 3.6 µs from local SQLite, 0.2 µs once cached
r.search("okapi sighting")            # 43 µs, with the matching line
r.write("/notes/plan.md", "...")      # goes to the server (versioned, audited), then updates the replica
```

- Measured at 20,000 files.
- Reads are as fresh as the last `sync()`; an incremental sync after one change took 4.3 ms.
- Files you lose access to are removed on the next sync.
- Local reads are sent as declared inputs on your next write, so trust labels still propagate.
- The replica file holds plaintext and is created with mode 0600. Protect it like the credential.
