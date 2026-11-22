# Multi-agent work

## Sharing

`cloud_share` gives another agent access to a path, optionally read-write, expiring, and
with a note it sees at its next session. An agent can pass on at most what it has: the
same or a narrower path, no extra rights, no looser approval policy, no later expiry.

## Leases

When several agents work in the same files, claim before writing:

```text
cloud_lease(path="/reports/", seconds=300, note="writing Q3")
→ You hold /reports/ for 300s (fence 7).
```

- Other agents writing under `/reports/` get `423 LEASE_HELD` with the holder's name and the time left.
- Acquiring your own lease again renews it and keeps the fence.
- `cloud_lease(action="list")` shows active leases; `action="release"` ends yours.
- Leases cover a file, a folder (path ending in `/`), or the whole workspace (`/`).
- **Fencing tokens.** Pass `fence=7` on writes. If your lease expired and someone else
  took over, the write is refused with `409 STALE_FENCE` instead of silently overwriting
  their work. This is the classic failure of long-running agents that pause, for example
  on a slow model call, while holding a lock.
- Humans are never blocked by leases.

## Atomic multi-file commits

```text
cloud_write(atomic=true, files=[
  {"path": "/cfg/app.json", "content": "...", "if_version": 4},
  {"path": "/cfg/db.json",  "content": "...", "if_version": 2}])
```

Either every file commits or none does. Combine with per-file `if_version` for an
optimistic multi-file compare-and-swap. If any file in the batch needs approval, nothing
is written (`ATOMIC_NEEDS_APPROVAL`); send those files without `atomic` instead.

## Delegation

An agent with share rights can create helper agents for sub-tasks:

```text
cloud_delegate(name="drafter", path="/drafts/", access="read_write", hours=2, max_writes=50)
```

- The helper is named `<parent>.<name>` and gets its own credential, returned once with
  an MCP config.
- Access is a subset of the parent's, enforced by the same rules as sharing.
- It expires (default 24 h, maximum 30 days).
- Its budget can't exceed what the parent has left on any limit the parent has.
- It inherits the parent's `ingests_untrusted` flag.
- Helpers can't create helpers of their own.
- `cloud_delegate(action="list")` shows helpers and their usage; `action="revoke"`
  disables one and anything below it.

## Kill switch

```bash
agentcloud agent kill research
# killed 3 agent(s) in 0.3 ms: research and 2 helper(s) it delegated
```

Or `POST /v1/agents/{id}/kill`. The agent and its whole delegation subtree are disabled,
and their credentials, grants and leases revoked. Every request re-checks the database,
so the next call from any of them gets `401`. Sub-agents also stop working the moment
their parent is disabled, even before the cascade runs.

## Budgets

```bash
agentcloud agent budget research --writes 500 --bytes 50MB --proposals 20
agentcloud agent budget research --reset     # zero the usage counters
agentcloud agent budget research --clear     # remove limits
```

Exceeding a limit returns `429 BUDGET_EXCEEDED` with the limit and usage. `cloud_whoami`
shows agents their own budget, so they can pace themselves. Budgets count Agent Cloud
operations; use your LLM gateway for token spend.

## Runaway detection

Independent of budgets, an agent is stopped with `429 RUNAWAY_SUSPECTED` when, within
10 minutes, it either:

- writes one file 60+ times while cycling through the same contents (A → B → A …), or
- writes one file 300+ times in any way.

An `agent.runaway` event fires (send it to Slack with a webhook). Appending to a log
produces new content each time, so it isn't flagged until the 300-write ceiling.
