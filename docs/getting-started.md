# Getting started

## 1. Install and run

```bash
pip install "agentcloud[fast] @ git+https://github.com/npmiaman/agent-cloud"   # Python 3.11+
agentcloud up
```

Or with Docker:

```bash
docker compose up -d && docker compose logs agentcloud
```

The first start creates a local admin, stores its key in `~/.agentcloud/admin.json`
(mode 0600) and prints a one-time link to the console. Run `agentcloud login-link` for
a new one.

## 2. Create an agent

```bash
agentcloud agent create research --storage 1GB
```

This creates the agent, its own workspace with a 1 GB quota, editor access and a
credential (`aca_…`, shown once). It also prints ready-to-paste configs:

```bash
# Claude Code
claude mcp add --transport http agent-cloud http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer aca_…"
```

```json
{ "mcpServers": { "agent-cloud": { "url": "http://127.0.0.1:8787/mcp",
  "headers": { "Authorization": "Bearer aca_…" } } } }
```

Options:
- `--access read_only|read_write|full`: `full` lets the agent share and delegate.
- `--approval none|overwrite|all`: route overwrites, or every change, through a human.

## 3. Use it

Ask your agent to save something. It already knows who it is and what it can reach, so
it just works with paths like `/research/notes.md`. Then:

```bash
agentcloud approvals          # changes waiting for you (if approval is on)
agentcloud status             # server, storage, agents
```

The console (the login link) shows the live log, files with diffs and restore,
approvals, credentials and webhooks.

## 4. From code

```python
from agentcloud import AgentCloud
ac = AgentCloud(token="aca_…", base_url="http://127.0.0.1:8787")
ws = ac.workspaces[0]                  # the agent's home workspace (or .get(workspace_id))
ws.write("notes.md", "# Findings\n")
print(ws.read_text("notes.md"))
```

TypeScript: see [`sdk/typescript`](../sdk/typescript). Shell: the `ac` command
(`ac ls`, `ac cat`, `ac put`) reads `AGENTCLOUD_URL` and `AGENTCLOUD_TOKEN`.

## Next

- [Agent interface](agent-interface.md): every tool your agent gets
- [Multi-agent work](multi-agent.md): several agents sharing files safely
- [Operations](operations.md): before you run it for real
