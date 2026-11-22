# Security

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | Yes |
| < 0.5 | No: upgrade (migrations run automatically on start) |

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub private vulnerability reporting](../../security/advisories/new) and include:

- the affected version or commit
- a minimal reproduction
- the impact you expect

We aim to acknowledge reports within 3 working days and to ship a fix or mitigation for
confirmed high-severity issues within 30 days. We credit reporters in the release notes
unless you ask us not to.

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
- **Prompt injection spreading through shared files (v0.5).**
  - Content derived from outside sources or untrusted files is labelled, and agents see it behind a warning banner.
  - Protected paths can refuse untrusted writes or send them for approval.
  - Provenance records are HMAC-signed, so tampering with the metadata database is detectable.
  - This reduces the risk; it does not eliminate it. A model can still follow instructions it was told to treat as data.
- **Runaway and over-delegated agents (v0.5).**
  - Budgets, runaway detection and leases limit the damage one agent can do.
  - Helper agents get a subset of their parent's access and expire.
  - The kill switch revokes an agent's whole delegation subtree on the next request.

**Does not defend (by design or not yet)**
- **Someone with the host.** Blob encryption uses a key stored in the data directory by default. Set `AGENTCLOUD_MASTER_KEY` from a secret manager for real separation.
- **Running agent code.** Agent Cloud stores files; it is not a sandbox. Don't execute untrusted files from it.
- **Webhook targets are not restricted.** Only organisation admins can create webhooks; restrict outbound traffic at the network layer if that matters to you.
- **Multi-host rate limiting.** Limits are per process until a Redis limiter exists.
- **Plain HTTP.** Put TLS in front (Caddy, nginx, a load balancer) for anything beyond localhost.
