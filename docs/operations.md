# Operations

## Running it

| Option | Command |
|---|---|
| Local | `agentcloud up [--port 8787] [--workers auto] [--data ~/.agentcloud]` |
| Docker | `docker compose up -d` (data in the `agentcloud-data` volume) |
| systemd / launchd | Units in [`deploy/`](../deploy) restart the server on crash |

`--workers auto` runs up to 4 processes; SQLite in WAL mode serialises writers across them.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENTCLOUD_DATA` | `~/.agentcloud` | Data directory: database, blobs, keys |
| `AGENTCLOUD_MASTER_KEY` | generated into `master.key` | Base64 32-byte key for blob encryption and signatures. Set it from a secret manager in production. |
| `AGENTCLOUD_DEFAULT_QUOTA` | 1 GiB | Default workspace quota, bytes (all versions count) |
| `AGENTCLOUD_MAX_UPLOAD` | 100 MiB | Largest single upload |
| `AGENTCLOUD_RATE_LIMIT` | 1200 | Requests per minute per credential (per process) |
| `AGENTCLOUD_AUTH_RATE_LIMIT` | 20 | Login attempts per minute per IP |
| `AGENTCLOUD_ACCESS_TOKEN_TTL` | 900 | Seconds an exchanged access token lives |
| `AGENTCLOUD_SESSION_TTL` | 7 days | Console session lifetime |
| `AGENTCLOUD_REPLICA_DIR` | unset | Second directory that receives a copy of every blob |
| `AGENTCLOUD_BACKUP_DIR` | unset | Enables daily verified backups there |
| `AGENTCLOUD_BACKUP_KEEP` | 7 | Backups to keep |
| `AGENTCLOUD_INLINE_WORKER` | 1 | Run maintenance inside the server; set 0 and run `agentcloud worker` separately |
| `AGENTCLOUD_ADMIN_KEY` | from `admin.json` | Admin key the `agentcloud` CLI uses |
| `AGENTCLOUD_EMBEDDER` | `none` | Vector search: `hashing`, `sentence-transformers:<model>` or `http:<url>\|<model>` ([docs](vector-search.md)) |
| `AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD` | `0` | Let a local model download its weights |
| `AGENTCLOUD_EMBEDDER_API_KEY` | unset | Bearer key for an `http:` embedder |

## Backups and recovery

```bash
agentcloud backup --to /mnt/backups      # consistent while running; every blob decrypted to verify
agentcloud restore /mnt/backups/agentcloud-20261123-120000-000 --force   # moves current data aside, never deletes it
```

Blobs are immutable, so backups on the same filesystem are hard links: instant and free.
A backup contains `master.key` unless you pass `--no-key`. Guard the backup directory, or
keep the key separately.

With `AGENTCLOUD_REPLICA_DIR` set, reads fall back to the replica and repair the primary.
The worker re-verifies blobs in rotation and repairs damage from the replica.

## Health

- `GET /healthz`: the process is up.
- `GET /readyz`: database and storage are usable.
- `GET /v1/metrics` (admin): request counts, latencies, rate limiting.

## Upgrades

Install the new version and restart. Schema migrations are additive and run on start.
Take a backup first; `agentcloud restore` brings you back.

## Production checklist

- [ ] TLS in front (Caddy, nginx or a load balancer). The server speaks plain HTTP.
- [ ] `AGENTCLOUD_MASTER_KEY` from a secret manager, not the data directory.
- [ ] `AGENTCLOUD_BACKUP_DIR` on a different disk, or `agentcloud backup` from cron.
- [ ] Webhooks for `approval.*`, `agent.runaway` and `trust.*` going somewhere a human sees.
- [ ] `ingests_untrusted` set on agents that read the web or email.
- [ ] Budgets on agents that run unattended.
- [ ] A trust policy protecting instruction and config paths.

## Limits

- Single node: SQLite metadata and local-disk blobs. Postgres and S3 backends are on the roadmap.
- Rate limits are per process.
- Bearer tokens only; OAuth for MCP isn't built yet.
