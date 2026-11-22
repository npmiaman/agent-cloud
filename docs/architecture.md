# Architecture

```
agents (MCP · SDKs · ac · memory tool)        console · agentcloud CLI
                     └──────────────┬──────────────┘
                     FastAPI (stateless, N processes)
      request IDs · rate limits · idempotency · fast JSON · agent context (_meta / headers)
   ┌────────────────────────────────┼─────────────────────────────────┐
 control plane                 coordination                       trust
 orgs/projects · agents        leases + fencing                   provenance records
 grants (allow/deny/scopes)    budgets · runaway detection        taint propagation
 versions · approvals · audit  delegation tree · kill switch      trust policy
                                    │
 data plane: upload sessions + signed URLs → AES-256-GCM chunked, content-addressed blob store
                                    │
 SQLite (WAL) metadata · local blobs (+ replica) · worker: expiry, GC, retention, scrub, backups, webhooks
```

## Modules

| Module | Responsibility |
|---|---|
| `auth.py` | Credentials, access tokens, the permission engine (allow ∪ − deny ∩ scopes) |
| `service.py` | Every file mutation: versions, approvals, quotas, audit, lease, budget and trust checks. Routes can't bypass it. |
| `provenance.py` | Signed provenance records, trust evaluation, quarantine and endorse |
| `coordination.py` | Leases with fencing tokens, budgets, runaway detection, delegation subtree and kill |
| `mcp.py` | MCP server: both protocol eras, tools, resources, session briefing |
| `agent_env.py` | Environment discovery, change briefings, BM25 search |
| `memory.py` | Claude memory-tool command execution |
| `dataplane.py` | Upload sessions and signed URLs |
| `storage.py` | Encrypted blob store; the seam for S3, R2 and GCS |
| `webhooks.py`, `worker.py` | Delivery with retries; maintenance |

## Decisions

- **Files have stable IDs.** A move changes the path in place, and grants, share links
  and pending approvals follow the file.
- **Versions are immutable.** Restores append; deletes are soft. Every mistake is undoable.
- **Agents never inherit a human's rights.** Explicit, expiring, revocable grants only;
  an agent can pass on at most what it has.
- **Trust is a property of content, computed at write time.** Each version's label
  depends on its inputs, so taint follows data through any number of agents and files.
  It's recomputed only when a human quarantines or endorses.
- **Everything is checked per request.** No cached authorisation, so revocation and kill
  take effect on the next call.
- **Stateless MCP.** Sessions are inferred from activity gaps, not connections, so any
  process can serve any request behind a plain load balancer.
- **Search decrypts in memory.** No plaintext index on disk; a per-blob chunk index is
  cached in memory and safe because blobs are immutable.

See [the research survey](research/agent-infrastructure-gaps.md) for why these
features, and the [CHANGELOG](../CHANGELOG.md) for what changed when.
