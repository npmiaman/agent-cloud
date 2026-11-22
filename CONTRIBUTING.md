# Contributing

Thanks for helping. The bar is simple: **every behaviour change comes with a test, and the
suite stays green.**

```bash
git clone https://github.com/npmiaman/agent-cloud && cd agent-cloud
make install        # pip install -e ".[dev,fast]"
make test           # Python suite (~25s)
make lint           # ruff
make sdk-ts         # TypeScript SDK build + e2e against a throwaway server
make bench          # before/after numbers for anything performance-related
```

## Workflow

1. For anything bigger than a small fix, open an issue or discussion first so we can agree on the approach.
2. Branch from `main`, keep pull requests focused, and fill in the PR template.
3. Add a line to `CHANGELOG.md` under **Unreleased**.
4. CI must pass: lint, tests on Python 3.11 to 3.13, the TypeScript SDK e2e, the package build and the Docker smoke test.

Commit messages: an imperative summary line ("Add lease renewal to cloud_lease"), then a
body explaining why when it isn't obvious.

## Layout

| Path | What |
|---|---|
| `agentcloud_server/auth.py` | Credentials, access tokens, the permission engine. Security-sensitive: tests first. |
| `agentcloud_server/service.py` | Every file mutation (versions, approvals, quotas, audit). Routes must not bypass it. |
| `agentcloud_server/dataplane.py` | Upload sessions + signed URLs. |
| `agentcloud_server/storage.py` | Encrypted, content-addressed blob store; the seam for new backends (S3, R2, GCS…). |
| `agentcloud_server/mcp.py`, `agent_env.py` | The agent-facing interface. |
| `agentcloud_server/provenance.py` | Signed provenance and trust labels. Security-sensitive. |
| `agentcloud_server/coordination.py` | Leases, budgets, runaway detection, delegation and kill switch. |
| `agentcloud_server/memory.py` | Claude memory-tool backend. |
| `agentcloud_server/webhooks.py`, `worker.py` | Background delivery and maintenance. |
| `agentcloud_server/cli.py` | The `agentcloud` command. |
| `agentcloud_server/dashboard/index.html` | The console: one dependency-free file. All user data goes through `textContent`, never `innerHTML`. |
| `sdk/python`, `sdk/typescript` | Client SDKs. Keep them dependency-light. |

## Good first contributions

- An `S3BlobStore` (implement `put_file` / `open_stream` / `exists` / `delete`), tested against MinIO.
- A Postgres driver for `db.py` (the SQL is portable apart from `json_extract` and `INSERT OR IGNORE`).
- Redis-backed rate limiting for multi-host deployments.
- More MCP clients verified in `README.md`.

## Ground rules

- Security issues: see `SECURITY.md`. Don't open public issues for them.
- Be kind: the [Code of Conduct](CODE_OF_CONDUCT.md) applies everywhere.
- No new runtime dependencies without discussion.
- Keep the agent interface small: a new MCP tool needs a strong reason.
