# @agentcloud/sdk

TypeScript SDK for Agent Cloud. Zero runtime dependencies, ESM, global `fetch` (Node 18+; browsers work for everything except local-path helpers).

```js
import { AgentCloud } from "@agentcloud/sdk";

const client = new AgentCloud({ apiKey: process.env.AGENT_CLOUD_KEY, baseUrl: "http://127.0.0.1:8787" });
const workspace = await client.workspaces.get("wsp_123");

const file = await workspace.files.upload({ path: "research/companies.csv", file: "./companies.csv" });
const files = await workspace.files.list({ path: "research/" });   // array + files.nextCursor
const text = await workspace.files.readText("research/companies.csv");
```

## Auth

`apiKey` can be a user API key (`acu_…`), a session (`acs_…`) or an agent credential (`aca_…`).
Agent credentials are exchanged at `POST /v1/auth/agent/token` for a short-lived `acx_` token, which is cached,
refreshed 60 s before expiry, and refreshed once more if a request gets a 401. `client.auth.state()` shows the
exchange count; `client.auth.invalidate()` drops the cached token.

Retries (network errors, 429, 502–504; default 2) apply to GETs and to writes, which always carry an
`Idempotency-Key`. A 401 is retried once with a fresh token, since the request was never processed.

## Files

All writes go through upload sessions: SHA-256 locally, `POST /uploads`, `PUT` the bytes to the signed URL,
`POST /complete` with the checksum. A mismatch throws `ChecksumMismatch`. Reads and downloads are checksum-verified as well.

| | |
|---|---|
| `files.upload({ path, file \| content, contentType?, ifVersion?, message? })` | `file` is a local path (Node) or bytes/Blob; `content` is bytes or UTF-8 text |
| `files.write(path, data, opts)` / `writeText` | text or bytes |
| `files.read(path, { version? })` / `readText` | by path |
| `files.download(pathOrId, { to?, version? })` | signed URL; `to` writes a local file atomically |
| `files.list({ path, recursive?, limit?, cursor? })`, `iterate()`, `listAll()` | cursor pagination |
| `files.search({ query?, ext?, createdBy?, modifiedAfter?, modifiedBefore? })` | name/metadata filters |
| `files.stat`, `exists`, `mkdir`, `move`, `rename`, `copy`, `delete` | |
| `files.versions`, `restore(path, n)`, `diff(path, from, to?)` | history |
| `files.share(pathOrId, { agentId, permission })`, `client.shares.revoke(id)` | per-file sharing |
| `workspace.search(q)`, `client.search(q)` | name + text-content search |

Writes return `{ pending: false, version, op, sha256, … }`, or `{ pending: true, proposalId }` when the
workspace needs human approval. Pending is a value, not an error; `workspace.waitFor(pending)` polls for the decision.

## Everything else

- `client.workspaces.create / list / get / ref / update / delete`
- `client.agents.create / provision / list / get / update / delete`, `client.agents.credentials.create / list / rotate / revoke`
- `client.approvals.list / get / approve / reject`
- `client.environment()`, `client.me()`, `client.home()`, `AgentCloud.signup()`, `AgentCloud.login()`, `client.apiKeys.*`
- `workspace.grant / grants / revokeGrant / proposals / activity`

## Errors

`AgentCloudError` has `status`, `code`, `requestId` and `details`. Subclasses: `BadRequest`, `Unauthenticated`, `Forbidden`,
`NotFound`, `Conflict`, `RateLimited` (`retryAfter`), `QuotaExceeded` and `ChecksumMismatch`.

## Build and test

```sh
npx -p typescript@5.6.3 tsc            # src/ -> dist/
node test/e2e.mjs                      # against $AGENTCLOUD_URL (default http://127.0.0.1:8799)
```

Uploads and downloads are buffered in memory, which is fine for the server's per-file limit but not for multi-GB files.
