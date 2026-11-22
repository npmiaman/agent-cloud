# Contributing

Thanks for helping. The bar is simple: **every behaviour change comes with a test, and the
suite stays green.**

```bash
git clone <this repo> && cd agent-cloud
pip install -e ".[dev]"
pytest -q                                   # Python suite (~20s)
agentcloud up --data /tmp/ac --port 8799 --workers 1 &
(cd sdk/typescript && npx -p typescript@5.6.3 tsc && node test/e2e.mjs)
python bench/bench.py                       # before/after numbers for anything performance-related
```

## Layout

| Path | What |
|---|---|
| `agentcloud_server/auth.py` | Credentials, access tokens, the permission engine. Security-sensitive: tests first. |
| `agentcloud_server/service.py` | Every file mutation (versions, approvals, quotas, audit). Routes must not bypass it. |
| `agentcloud_server/dataplane.py` | Upload sessions + signed URLs. |
| `agentcloud_server/storage.py` | Encrypted, content-addressed blob store; the seam for new backends (S3, R2, GCS…). |
| `agentcloud_server/mcp.py`, `agent_env.py` | The agent-facing interface. |
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
- No new runtime dependencies without discussion.
- Keep the agent interface small: a new MCP tool needs a strong reason.
