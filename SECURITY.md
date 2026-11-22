# Security

## Reporting

Please report vulnerabilities privately (GitHub security advisory on this repository)
rather than in a public issue. Include a reproduction; we'll acknowledge within a few days.

## Threat model (what Agent Cloud defends, and what it doesn't)

**Defends**
- **Agents exceeding their access.**
  - Every operation is authorised server-side: allow grants, then deny rules, then credential scopes.
  - Agents can't create agents, change permissions beyond re-sharing a subset of their own, or approve changes.
- **Cross-tenant access.** Organisations are isolated in the database, in blob storage namespaces and in signed URLs. A resource you can't access returns 404.
- **Destructive mistakes.**
  - Versions are immutable, deletes are soft, and restores append a new version.
  - Approvals re-check that the file hasn't changed since the proposal.
- **Credential theft blast radius.**
  - Credentials are hashed at rest, scoped, expiring and rotatable.
  - Access tokens last 15 minutes, and revocation is immediate.
  - Share-link tokens can't authenticate API calls.
- **Untrusted agent output in the console.** Files are served sandboxed, and the dashboard never renders agent data as HTML.
- **Path tricks:** traversal, control characters and dot segments are rejected.

**Does not defend (by design or not yet)**
- **Someone with the host.** Blob encryption uses a key stored in the data directory by default. Set `AGENTCLOUD_MASTER_KEY` from a secret manager for real separation.
- **Running agent code.** Agent Cloud stores files; it is not a sandbox. Don't execute untrusted files from it.
- **Webhook targets are not restricted.** Only organisation admins can create webhooks; restrict outbound traffic at the network layer if that matters to you.
- **Multi-host rate limiting.** Limits are per process until a Redis limiter exists.
- **Plain HTTP.** Put TLS in front (Caddy, nginx, a load balancer) for anything beyond localhost.
