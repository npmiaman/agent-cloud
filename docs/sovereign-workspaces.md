# Agent-sovereign workspaces

At a million agents, humans become the bottleneck: nobody can approve every share or
review every change. So in Agent Cloud **each agent owns its workspaces and runs them**:

- it decides who else can read or write them, and denies what it wants kept private
- it reviews other agents' proposed changes
- it creates new isolated workspaces out of its own storage
- it versions, branches and rolls back its own work

Humans write one constitution for the organisation, and the defaults are safe enough that
they don't have to. After that they only handle exceptions.

## Ownership

`agentcloud agent create NAME` gives the agent the **owner** role on its home workspace
(the default since v0.6; `--access read_write` for a non-owner). In the API that's
`POST /v1/agents/provision` with `"access": "owner"`.

An owner agent gets three extra tools:

| Tool | What it does |
|---|---|
| `cloud_permissions` | `list` who has access; `grant` an agent `read`, `read_write` or `full` on a path, optionally expiring and optionally with `approval=all` so the owner reviews their changes; `deny` an agent a path; `revoke` a grant |
| `cloud_review` | `list` proposed changes to workspaces it owns (`diffs=true` includes diffs); `approve` or `reject` with a note |
| `cloud_workspace` | `list` workspaces it owns; `create` a new private workspace with `storage_mb` carved from its home workspace's quota; `settings` to set a workspace's trust policy |

A typical loop with no human in it:

```text
owner:    cloud_permissions(action="grant", agent="writer", path="/drafts/", access="read_write", approval="all")
writer:   cloud_write(workspace="owner", path="/drafts/report.md", content="...")   → waiting for review
owner:    cloud_review(action="list", diffs=true)
owner:    cloud_review(action="approve", proposal_id="prop_…", note="good, ship it")
writer:   (next session) "approved by owner: good, ship it"
```

## The constitution

```bash
agentcloud policy                                  # show
agentcloud policy max_grant_hours=48 'grantable=["read","write"]' cross_project=false
```

Or `GET` / `PATCH /v1/organisation/agent-policy`. Agents can read it; only organisation
admins can change it.

| Key | Default | Meaning |
|---|---|---|
| `agents_govern` | `true` | Owner agents may grant, deny and revoke in their workspaces |
| `agent_review` | `true` | Owner agents may approve or reject other agents' proposals |
| `agents_create_workspaces` | `true` | Owner agents may carve new workspaces from their own quota |
| `grantable` | `["read","write","share"]` | Rights an agent may give others; `delete` is opt-in |
| `max_grant_hours` | `720` | Longest grant an agent may issue (`null` = no limit) |
| `cross_project` | `true` | May grant to agents in other projects |
| `untrusted_can_grant` | `false` | Whether an agent that recently read untrusted content may widen access |
| `max_workspaces_per_agent` | `50` | Workspaces one agent may own |

## Why this is safe when agents can be tricked

- **Limits come from the constitution,** not from the agent's judgment.
- **Poisoned agents can't widen access.** An agent whose recent reads include untrusted
  content gets `403 UNTRUSTED_CONTEXT` when it tries to grant. Narrowing (deny, revoke)
  is always allowed. Reads are scoped per task when the client sends a task ID.
- **No self-approval, no self-promotion.** An agent can't approve its own proposals,
  change its own access, or grant ownership.
- **Private by default.** A new workspace is invisible to every other agent until its owner grants access.
- **Everything is audited,** and the kill switch disables an owner and every helper it created.
- **Humans keep break-glass access** to everything in their organisation.

## Version control

Every agent can version its whole workspace, not just single files.

| `cloud_commit` action | |
|---|---|
| `create` | Snapshot the workspace now. Instant: a commit records a sequence number and copies nothing. |
| `log` | List commits |
| `diff` | What was added, changed, removed and renamed between two commits (or a commit and now) |
| `rollback` | Make the workspace exactly match a commit. History is kept, so the rollback can be undone. |

| `cloud_branch` action (owners) | |
|---|---|
| `create` | Fork a workspace into a private branch named `<workspace>.<name>`. Storage is shared; only metadata is written. |
| `list` | Branches with pending changes and conflicts |
| `preview` | What a merge would do |
| `merge` | Three-way merge back into the parent, atomically. Conflicts must be resolved per path with `resolve={"/x.md": "ours"\|"theirs"}`. |

Rollbacks and merges go through the same checks as any write: permissions, approval
policies, leases, budgets and trust labels (untrusted content stays untrusted on a branch
and after a merge).

### How it works

Every mutation in a workspace takes the next value of a per-workspace sequence number, and
a change log records which file it touched. Each commit stores a **Merkle tree**: one
content-addressed object per folder.

- **Commits:** the first builds the whole tree (155 ms at 20,000 files). Later commits only
  rewrite the folders on changed paths (2.1 ms).
- **Diffs and rollbacks:** compare tree hashes and skip identical subtrees, so their cost
  tracks the change, not the workspace (0.2 ms at 20,000 files).
- **Branches:** a metadata copy pointing at the same encrypted blobs.
- **Merges:** three-way by path. Text files edited on both sides are merged line by line (diff3).

## Limits

- Adjacent edits (neighbouring lines changed on both sides) conflict, as in git. Resolve
  them with `ours`, `theirs` or `{"content": ...}`.
- Rename detection works within a workspace (files keep their IDs across moves). Across a branch and its parent, a rename shows up as a delete plus an add.
- Merging still compares full snapshots of the branch and its parent, which is linear in the number of files.
