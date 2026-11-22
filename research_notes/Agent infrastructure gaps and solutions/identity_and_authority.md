# Identity, Delegation and Authority Management for Autonomous AI Agents (2025-2026)

Research date: 2026-11-22. I made 15 search/fetch calls. Pages I fetched and read in full: the MCP authorization spec (2025-11-25) and the Microsoft Entra Agent ID governance overview (updated 2026-11-17). Everything else comes from search-result summaries of primary pages: IETF datatracker, vendor blogs and docs. I note where a claim rests only on vendor marketing or on an aggregator.

Agent Cloud baseline used for gap analysis (from the assignment brief, not re-checked against code): per-agent credentials hashed at rest, scopes, 15-min access-token exchange, rotation with grace, path-level allow/deny grants, agents can re-share only a subset of their own access.

---

## 1. Delegated / task-scoped credentials and delegation chains (agent -> sub-agent)

### Takeaway
The building blocks are standardised: RFC 8693 token exchange (`act` claim, actor tokens), transaction tokens and attenuable tokens like Biscuit. Agent-specific delegation work is still mostly individual IETF drafts with no working-group adoption. The one agent-relevant OAuth extension in real production is ID-JAG / Cross App Access. Nobody has standardised a multi-hop agent -> sub-agent chain with budgets.

### Cited Findings
- **RFC 8693 token exchange is the base primitive vendors build on.** Keycard's May 2026 multi-agent product uses RFC 8693 token exchange, checks policy on every exchange and narrows permissions at each hop, "so no agent ever holds more". Its agents carry no long-lived API keys or on-disk credentials. — [Help Net Security, 2026-05-15](https://www.helpnetsecurity.com/2026/05/15/keycard-for-multi-agent-apps/)
- **draft-oauth-ai-agents-on-behalf-of-user** (Senarath & Dissanayaka):
  - Extends the authorization-code flow so an agent can get delegated tokens for a user.
  - -00 (May 2025) added a `requested_agent` parameter. -01 renamed it `requested_actor` and added an `actor_token` so the agent authenticates itself.
  - WorkOS reports a -02 (Aug 2025), not adopted by the OAuth WG. It covers only one hop, user -> agent.
  - Sources: [datatracker -01](https://datatracker.ietf.org/doc/draft-oauth-ai-agents-on-behalf-of-user/01/); [WorkOS blog](https://workos.com/blog/oauth-on-behalf-of-ai-agents)
- **Transaction Tokens for Agents** (draft-oauth-transaction-tokens-for-agents, -00 to -05 on datatracker; one commentator mentions -06):
  - Extends IETF Transaction Tokens (short-lived tokens that carry identity and context through a call chain inside a trust domain).
  - Adds an `actor` (the agent, carried in the `act` claim) and a `principal` (the initiating human or system). The principal MAY be omitted for autonomous agents.
  - Replacement Txn-Tokens MUST NOT replace the principal context.
  - Sources: [draft -04](https://www.ietf.org/archive/id/draft-oauth-transaction-tokens-for-agents-04.html); [datatracker -05](https://datatracker.ietf.org/doc/draft-oauth-transaction-tokens-for-agents/05/)
- **Other individual drafts:**
  - Yao et al., "Further considerations on AI Agent Authentication and Authorization" (June 2025): agents may act on behalf of users, themselves, or other agents. — [IETF](https://www.ietf.org/archive/id/draft-yao-agent-auth-considerations-00.html)
  - AAuth, "Agentic Authorization OAuth 2.1 Extension" (Rosenberg & White, July 2025): listed as expired. — [datatracker](https://datatracker.ietf.org/doc/html/draft-rosenberg-oauth-aauth-00)
  - Song, "Applier-On-Behalf-Of" (Nov 2025): an `applier_id` lets a lead agent request tokens for sub-agents. — [IETF](https://www.ietf.org/archive/id/draft-song-oauth-ai-agent-collaborate-authz-00.html)
- **draft-asor-wimse-agent-delegation-chain** (-00 Aug 2026, -01 Sep 2026): an "Agent Delegation Chain" draft under WIMSE (workload identity). I saw only the title and dates in search results. I did NOT read the content. — [datatracker -00](https://datatracker.ietf.org/doc/html/draft-asor-wimse-agent-delegation-chain-00); [-01](https://www.ietf.org/archive/id/draft-asor-wimse-agent-delegation-chain-01.html)
- **ID-JAG / Cross App Access (XAA)** (Identity Assertion JWT Authorization Grant):
  - Flow: during SSO the client gets an ID-JAG from the enterprise IdP via a token-exchange grant. The IdP applies admin policy first. The client then exchanges the ID-JAG at the resource's authorization server for an access token.
  - The ID-JAG is short-lived and audience-bound. Okta's example shows a 300 s expiry.
  - Okta brands this as Cross App Access. Auth0 documents XAA as a Resource App option.
  - Sources: [Okta dev blog](https://developer.okta.com/blog/2025/06/23/enterprise-ai); [WorkOS on ID-JAG](https://workos.com/blog/id-jag-cross-app-access); [Auth0 docs](https://auth0.com/docs/ai-agents-mcp/cross-app-access)
- **OpenID Foundation AI Identity Management Community Group** published "Identity Management for Agentic AI" on 2025-10-07 (arXiv 2510.25819, lead author Tobin South).
  - Finding: existing SSO and user management cover simple agent cases but break down for autonomous agents.
  - Flagged problems: inconsistent identity across companies, difficulty separating user actions from delegated agent actions, no scalable oversight.
  - It recommends rigorous, interoperable profiles of existing standards rather than new protocols. It notes OAuth/OIDC were built for individual-user authorization, while agents may run in shared codebases or group channels.
  - Sources: [OpenID announcement](https://openid.net/new-whitepaper-tackles-ai-agent-identity-challenges/); [arXiv](https://arxiv.org/abs/2510.25819)
- **Biscuit** (biscuit-auth, crate v6.x):
  - Any holder can attenuate a token offline, without contacting the issuer. Each appended block can only add restrictions.
  - Verification uses a public key, so verifiers need only the root public key. Macaroons need a shared root secret.
  - Third-party blocks let an outside party sign a block that the holder appends.
  - Any failing check invalidates the whole token.
  - Sources: [docs.rs biscuit-auth 0.8](https://docs.rs/crate/biscuit-auth/0.8.0); [third-party example, v6.0.0](https://docs.rs/crate/biscuit-auth/6.0.0/source/examples/third_party.rs)
  - I found no primary source showing a shipping agent product built on Biscuit or Macaroons.
- **SPIFFE/SPIRE** (CNCF graduated) gives workload identity (X.509/JWT SVIDs), not authorization of the action. Aembit says plainly that SPIFFE "does not inherently give you an access management layer for workloads or AI agents".
  - Aembit added a SPIFFE credential provider (JWT-SVID) in Nov 2025 (KubeCon).
  - Sources: [Aembit press release](https://aembit.io/press-release/aembit-introduces-spiffe-credential-provider-to-extend-secure-workload-authentication-across-cloud-native-and-ai-environments/); [Aembit blog on SPIFFE cost](https://aembit.io/blog/everyone-wants-spiffe-almost-no-one-can-afford-to-build-it-right/)
- A 2026 survey (arXiv 2604.23280): applying unmodified OAuth to agents forces either impersonation (the agent replays the user's token) or overly broad static API keys. — [arXiv](https://arxiv.org/pdf/2604.23280)

### Inferences
- How the field converges: RFC 8693 token exchange with `act` chains for online narrowing; transaction tokens for context propagation inside a trust domain; ID-JAG for enterprise-IdP-governed cross-app grants. Offline attenuation (Biscuit-style) is technically mature but not adopted by agent vendors.
- None of the drafts I found standardise **budgets** (spend, call count, data volume) or **task binding** as first-class constraints in a delegated credential. Delegation narrows scope and audience only.
- Agent Cloud's "re-share only a subset" rule is already an attenuation model. It is effectively token exchange with a subset check. Agent Cloud is at least aligned with where vendors like Keycard are, at least in concept.

### Gaps
- Current revision and status of most agent drafts: many show as expired, and I did not check the datatracker page for each.
- An arXiv paper reportedly cites "draft-klrc-aiagent-auth-03 (July 2026)" as an individual submission. I did not verify this draft exists.
- I did not confirm whether the IETF ID-JAG draft has been adopted by the OAuth WG, or its exact name and revision.
- Content of the WIMSE agent-delegation-chain draft: not read.

---

## 2. Agent identity startups and products: what they actually ship

### Takeaway
Three big IdPs now ship agent identity:
- Microsoft Entra Agent ID is the most complete on lifecycle and governance: sponsors, expiring access packages, an inactivity auto-disable, blueprint-level kill.
- Auth0 for AI Agents went GA in Nov 2025: Token Vault, CIBA async approval, FGA for RAG.
- Okta owns the Cross App Access / ID-JAG standard.

Among startups, Keycard (token exchange, per-task tokens, secret-swapping proxy) and Arcade (token injection into tool calls) are the clearest "agent never holds the credential" products. Several named vendors went unchecked within my call budget.

### Cited Findings
- **Microsoft Entra Agent ID**, read from the docs updated 2026-11-17:
  - **Objects:** four new types: agent identity blueprint, blueprint principal, agent identity, and agent user. Agent identities are created from a blueprint and can inherit OAuth delegated scopes from it. — [MS Learn governance overview](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
  - **Access packages:** access comes through entitlement-management access packages covering groups, OAuth API permissions and Entra roles. Three ways to request: the agent requests programmatically via `accessPackageAssignmentRequest`, its sponsor requests on its behalf, or an admin assigns directly. — [same](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
  - **Expiry:** when an assignment has an expiry date, the sponsor is notified in advance and can request an extension, which can trigger a new approval cycle. If the sponsor does nothing, access auto-expires. — [same](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
  - **Inactivity:** a default inactivity policy applies to all tenants. It finds inactive agent identities, notifies people, then disables the identity if no activity follows. — [same](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
  - **Sponsors:** at least one sponsor is required per agent identity and blueprint (blueprint principals are exempt at creation). Sponsors can enable, disable, change sponsor or soft-delete, but cannot edit credentials or config; owners can.
    - If a sponsor leaves, sponsorship transfers automatically to their manager. Lifecycle Workflows add notify-manager and notify-cosponsor tasks.
    - Sources: [owners/sponsors/managers](https://learn.microsoft.com/en-us/entra/agent-id/agent-owners-sponsors-managers); [sponsor tasks](https://learn.microsoft.com/en-sg/Entra/id-governance/agent-sponsor-tasks)
  - **Blueprint kill:** disabling a blueprint stops new agent identities being created from it and blocks the existing ones. — [MS Learn (search summary)](https://learn.microsoft.com/en-us/entra/agent-id/agent-identities)
  - **Other controls:** Conditional Access and ID Protection risk scoring apply to agents and can be set at blueprint level. Detection of overprivileged agent NHIs ("dynamic permissions") is in preview. — [governance overview](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
  - **Licensing:** Microsoft 365 E7, or Agent 365 plus Entra P1 / M365 E3. — [same](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
- **Auth0 for AI Agents:** GA on 2025-11-19; developer preview from April 2025.
  - Four features: User Authentication, Token Vault, Asynchronous Authorization, FGA for RAG.
  - Token Vault stores and refreshes third-party OAuth tokens: 30+ or 35+ integrations depending on the source.
  - Async authorization uses CIBA so agents get human approval out of band. Guardian push is live; email/SMS are "coming soon" per the official post. The DEV post contradicts this, saying email is available.
  - Sources: [Auth0 GA post](https://auth0.com/blog/auth0-for-ai-agents-generally-available/); [DEV post](https://dev.to/auth0/auth0-for-ai-agents-is-now-generally-available-29el)
- **Okta Cross App Access (XAA):**
  - The ID-JAG flow above, adopted as the MCP "Enterprise-Managed Authorization" extension, which the MCP blog calls stable.
  - Adopted by Anthropic, Microsoft and Okta per MCP's announcement. Okta has a Claude Enterprise-managed auth beta.
  - Sources: [MCP blog](https://blog.modelcontextprotocol.io/posts/enterprise-managed-auth/); [Okta newsroom](https://www.okta.com/en-ca/newsroom/articles/cross-app-access-extends-mcp-to-bring-enterprise-grade-security-to-ai-agents/); [Okta support beta guide](https://support.okta.com/help/s/article/claude-enterprise-managed-auth-with-okta-cross-app-access-xaa-beta-participation-guide)
- **Keycard:**
  - Came out of stealth in Oct 2025 with $38M, per WorkOS, a competitor.
  - Its token server issues short-lived, task-scoped tokens after a per-request policy check. Denials are logged and get no credential.
  - A sidecar proxy swaps placeholder secrets for real tokens, so the agent never holds a real credential.
  - "Keycard for Coding Agents" issues credentials per tool call. The May 2026 multi-agent product does RFC 8693 narrowing per hop.
  - It federates existing IdPs (Okta, Entra, Google, Auth0).
  - Mostly vendor-sourced. Sources: [keycard.ai](https://keycard.ai/); [coding agents post](https://keycard.ai/blog/announcing-keycard-for-coding-agents/); [Help Net Security](https://www.helpnetsecurity.com/2026/05/15/keycard-for-multi-agent-apps/); [WorkOS comparison](https://workos.com/blog/keycard-vs-workos-agent-credentials-enterprise-authentication)
- **Aembit:** workload and agent IAM.
  - SPIFFE JWT-SVID provider (Nov 2025).
  - "Blended Identity" evaluates agent identity and human operator identity together in one policy decision at request time.
  - Sources: [Aembit press release](https://aembit.io/press-release/aembit-introduces-spiffe-credential-provider-to-extend-secure-workload-authentication-across-cloud-native-and-ai-environments/); Blended Identity is from an aggregator summary ([hackread](https://hackread.com/astrix-aembit-alternatives-ai-agent-identity-security/)), not verified at a primary source.
- **Astrix:**
  - NHI discovery and posture (SaaS OAuth grants, API-key sprawl). "Agent Control Plane" launched Sept 2025.
  - A reported Cisco acquisition is unverified, and sources conflict.
  - Source (aggregator only): [hackread](https://hackread.com/astrix-aembit-alternatives-ai-agent-identity-security/)
- **Arcade.dev:** OAuth tokens and API keys are injected into tool calls at runtime. "The client and the LLM never see the secret values." Tools fetch the token via `context.get_auth_token_or_empty()`. — [Arcade docs](https://docs.arcade.dev/en/home/build-tools/create-a-tool-with-auth); [Arcade authorization page](https://www.arcade.dev/get-started/authorization/)
- **WorkOS:** publishes extensively on agent OAuth (OBO, ID-JAG, MCP auth), and its blog positions it against Keycard. I did not verify specific agent-product features from WorkOS docs. — [WorkOS blog](https://workos.com/blog/mcp-enterprise-managed-authorization-id-jag)

### Inferences
- Microsoft is the reference design for long-horizon governance: sponsors, expiring grants, inactivity auto-disable, manager reassignment. All of it sits behind M365 E7 or Agent 365 licensing and is tied to the Entra tenant. There is no self-hosted equivalent.
- Auth0's and Arcade's value is the third-party token vault plus human approval. Keycard's value is per-task token minting plus a secret-swapping proxy. That proxy pattern is the closest analogue to what Agent Cloud could do inside its own data plane.

### Gaps
- I did not check, within budget, Stytch, Descope, Clerk, Composio's auth model, Anon, or Infisical's agent features. Make no claims about them from these notes.
- I did not check Okta's broader "Okta for AI Agents" / ISPM agent discovery features.
- Pricing and adoption numbers for any vendor: not found.

---

## 3. MCP authorization spec and delegation

### Takeaway
MCP 2025-11-25 is a clean OAuth 2.1 resource-server profile:
- Protected Resource Metadata is mandatory.
- Client ID Metadata Documents are preferred over DCR.
- RFC 8707 audience binding, PKCE S256 and scope step-up are required or recommended.
- Token passthrough is explicitly forbidden.

It has no delegation model. A downstream call means the MCP server becomes a separate OAuth client with a separate token, and agent -> sub-agent chains are out of scope. Enterprise policy comes through the separate Enterprise-Managed Authorization (ID-JAG) extension.

### Cited Findings
All facts in this section come from the [MCP Authorization spec, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization), fetched and read in full, unless another source is linked.

- **Scope of the spec:**
  - Authorization is OPTIONAL. HTTP transports SHOULD conform. STDIO transports SHOULD NOT; they take credentials from the environment.
  - Based on OAuth 2.1 (draft-ietf-oauth-v2-1-13), RFC 8414, RFC 7591, RFC 9728 and draft-ietf-oauth-client-id-metadata-document-00.
- **Discovery:** MCP servers MUST implement RFC 9728 Protected Resource Metadata with `authorization_servers`. Discovery is via the `WWW-Authenticate` `resource_metadata` value or a well-known URI.
- **Client registration**, in client priority order:
  1. Pre-registration.
  2. Client ID Metadata Documents (CIMD): an HTTPS URL is the client_id, and the AS fetches that document.
  3. DCR, kept "for backwards compatibility".
- **Known CIMD weaknesses the spec itself lists:**
  - SSRF risk for the AS that fetches the document.
  - CIMD "cannot prevent localhost URL impersonation". An attacker can present a legitimate client's metadata URL plus their own localhost redirect.
- **Audience binding:** clients MUST send the RFC 8707 `resource` parameter in authorization and token requests. Servers MUST validate the audience and MUST NOT accept or transit other tokens. When the MCP server calls upstream APIs it acts as a separate OAuth client and "MUST NOT pass through the token it received".
- **Scopes:**
  - If the 401 challenge has no `scope`, clients request all of `scopes_supported`, which the spec frames as the minimal set.
  - At runtime the server returns 403 `insufficient_scope`, and the client SHOULD do step-up re-authorization with retry limits.
  - `client_credentials` clients (agents acting for themselves) MAY step up or abort.
- **Token hygiene:** short-lived access tokens are a SHOULD. Refresh-token rotation is a MUST for public clients. PKCE S256 is a MUST; clients refuse to proceed if the AS metadata lacks `code_challenge_methods_supported`.
- **Proxies:** MCP proxy servers using static client IDs MUST get user consent for each dynamically registered client (confused-deputy mitigation).
- **Extensions:** live in a separate `ext-auth` repo. Enterprise-Managed Authorization (ID-JAG/XAA) is marked stable. — [MCP blog](https://blog.modelcontextprotocol.io/posts/enterprise-managed-auth/)

### Inferences
- MCP's model is "user (or client) -> one MCP server". It has no notion of:
  - agent identity distinct from the client app,
  - delegation chains or the `act` claim,
  - per-task or budget-bound tokens,
  - re-authorization cadence.
- The "request all of `scopes_supported`" default plus long-lived refresh tokens pushes toward broad, durable grants. That is the opposite of authority decay.
- Agent Cloud's MCP server can comply cheaply: publish PRM, require `resource`/audience, and its 15-min tokens already exceed the "short-lived SHOULD". It can then add value above the spec through delegation and budget claims.

### Gaps
- I did not check whether any MCP SEP (spec enhancement proposal) is in flight for agent identity or delegation chains.
- Real-world CIMD adoption rates: not found.

---

## 4. Keeping secrets out of model context

### Takeaway
"Agents never see tokens" is now a common product claim. It is done three ways:
- server-side injection into tool execution (Arcade, Auth0 Token Vault);
- a proxy that swaps placeholders for real secrets (Keycard sidecar);
- browser-level autofill gated by human approval (1Password Secure Agentic Autofill via Browserbase).

All of them protect the secret value but not the authority. An agent with injected access can still misuse it.

### Cited Findings
- **Arcade:** secrets are injected at tool-call runtime. The LLM sees only tool definitions and results, never tokens. Arcade handles the OAuth flow and refresh. — [Arcade docs](https://docs.arcade.dev/en/home/build-tools/create-a-tool-with-auth); [Arcade authorization page](https://www.arcade.dev/get-started/authorization/)
- **Keycard:** a sidecar proxy swaps placeholder secrets for real tokens, so the agent "never holds a real credential". — [keycard.ai](https://keycard.ai/)
- **1Password Secure Agentic Autofill:** early access since Oct 2025 via Browserbase Director.
  - Delivers end-to-end-encrypted, just-in-time credentials through the 1Password browser extension, only when needed and always authorised by a person.
  - "The AI agent and underlying LLM never need to see nor handle the credentials."
  - Browser-only.
  - Sources: [1Password blog](https://1password.com/blog/closing-the-credential-risk-gap-for-browser-use-ai-agents); [1Password press release](https://1password.com/press/2025/oct/browserbase-ai-security-partnership)
- **Auth0 Token Vault:** a centralised layer that stores and exchanges third-party API tokens for agents. — [Auth0 GA post](https://auth0.com/blog/auth0-for-ai-agents-generally-available/)
- **MCP spec:** forbids token passthrough and requires secure token storage. For STDIO servers it pushes credentials into the environment, which in practice means env vars that agents and LLM tools can often read. — [MCP spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

### Inferences
- Agent Cloud already has a data plane, so it can be the injection point. It can resolve `secret://` references at execution time inside its own storage and MCP layer, the way Keycard's proxy does, so raw values never appear in tool output returned to the model.
- Gap no one closes: a secret injected into a sandbox the agent controls (shell, env) can be exfiltrated by the agent. Only proxy or egress-side injection keeps the value outside the agent's process.

### Gaps
- Composio's auth model, Infisical's agent features and 1Password's other agent work (SDK, service accounts for agents) were not verified within budget.
- I found no independent security evaluation of any "never sees the token" claim. All the claims are vendor-sourced.

---

## 5. Authority decay over long horizons

### Takeaway
Only Microsoft ships agent-specific decay controls as product:
- mandatory human sponsor, auto-reassigned to the manager when the sponsor leaves;
- expiring access packages that lapse unless the sponsor extends them;
- a default inactivity auto-disable;
- blueprint-level disable.

Generic cloud IAM has unused-access detection (AWS IAM Access Analyzer) but nothing agent-aware. Kill switches and "zombie agent" cleanup exist only as guidance in vendor blogs, not as standards.

### Cited Findings
- **Entra Agent ID:** sponsor-required, sponsor -> manager transfer on departure, expiry notifications with auto-expiry on sponsor inaction, default inactivity policy that notifies then disables, blueprint disable that blocks all child identities. — [MS Learn governance overview](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview); [owners/sponsors/managers](https://learn.microsoft.com/en-us/entra/agent-id/agent-owners-sponsors-managers)
- **AWS IAM Access Analyzer unused access** (launched Nov 2023, so older than 2025):
  - Periodically scans IAM roles and users and flags unused roles, access keys, passwords and permissions within a configurable usage window.
  - Recommends deleting roles and deactivating keys. Charged per role/user analysed.
  - Not agent-specific.
  - Sources: [AWS docs](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-findings.html); [AWS what's new 2023](https://aws.amazon.com/about-aws/whats-new/2023/11/iam-access-analyzer-inspecting-unused-access)
- **Orphaned-agent risk** (vendor blog content):
  - Agents whose creator account was disabled keep running with valid credentials. If the creator was an admin, the agent keeps admin-level authority.
  - "Zombie agents" persist through group memberships, API tokens and scheduled triggers even after the agent is disabled in the UI.
  - Sources: [Obsidian Security](https://www.obsidiansecurity.com/academy/orphaned-unsanctioned-ai-agents); [No Jitter](https://www.nojitter.com/ai-automation/why-you-need-a-retirement-plan-for-orphaned-ai-agents)
- **Kill-switch guidance** (non-standard, practitioner and vendor sources):
  - A kill switch must revoke credentials, stop running jobs and remove downstream permissions, not just flip a status. Kill authority must sit outside the agent's own authority.
  - Drill it on a schedule and measure how long tokens keep working after the kill.
  - Recurring reviews should compare granted authority against observed calls and denials, and confirm the owner still exists.
  - Sources: [nhimg.org](https://nhimg.org/articles/ai-kill-switches-must-sit-outside-agent-authority-boundaries); [DEV: access review checks](https://dev.to/ankush1982/add-ai-agents-to-your-access-review-in-three-checks-owner-scope-stop-and-evidence-hg7)
- **OpenID whitepaper:** names "lack of scalable oversight" as a key gap for autonomous agents. — [OpenID Foundation](https://openid.net/new-whitepaper-tackles-ai-agent-identity-challenges/)
- **Auth0 CIBA async authorization:** a per-action human-approval mechanism, usable as a step-up for high-risk actions in long-running agents. — [Auth0 GA post](https://auth0.com/blog/auth0-for-ai-agents-generally-available/)

### Inferences
- Decay is tied to the human org chart (Entra) or done as after-the-fact analytics (AWS). Neither is built into the credential itself.
- No product I found propagates decay down a delegation chain. When a parent agent's sponsor leaves or its grant expires, nothing cascades the pause or expiry to sub-credentials the agent minted.
- The kill switch's weak point is token lifetime and offline-verifiable tokens. Agent Cloud's 15-min access tokens bound the post-kill window to at most 15 min. That number is worth publishing as a measured property.

### Gaps
- I found no standard (IETF/OpenID) for agent re-authorization cadence, dead-man switches or kill switches.
- Okta and Google agent lifecycle features were not checked.
- No primary data found on how often orphaned agents occur in practice. Obsidian's claims are vendor content without disclosed data.

---

## 6. Where the gaps are, and how Agent Cloud could fill them

### Takeaway
Nobody does all of the following well, and nobody self-hosted does any of them:
1. Agent-minted attenuated sub-credentials carrying budgets, task binding and expiry, not just scope.
2. A first-class, queryable delegation-chain audit (who minted what, for whom, why).
3. Decay that cascades: owner-of-record, re-auth cadence and auto-pause propagating down the chain.
4. Kill switches with a measured revocation latency.

Microsoft has items 3 and 4 for flat identities inside Entra. Keycard has item 1 (narrowing only) as SaaS. Agent Cloud's existing primitives map closely onto all four.

### Cited Findings
- The OBO draft covers only one hop (user -> agent). — [WorkOS](https://workos.com/blog/oauth-on-behalf-of-ai-agents)
- Transaction-tokens-for-agents carries actor and principal but does not define budgets or a multi-hop chain record. — [draft -04](https://www.ietf.org/archive/id/draft-oauth-transaction-tokens-for-agents-04.html)
- Keycard narrows permissions per hop via token exchange (SaaS, federates an external IdP). — [Help Net Security](https://www.helpnetsecurity.com/2026/05/15/keycard-for-multi-agent-apps/)
- Entra's sponsor, expiry and inactivity controls exist but require M365 E7 or Agent 365 licensing. — [MS Learn](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)
- MCP has no delegation or agent-identity model and forbids passthrough. — [MCP spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- Biscuit supports offline holder attenuation with public-key verification. — [docs.rs](https://docs.rs/crate/biscuit-auth/0.8.0)

### Inferences
These are concrete proposals mapped onto Agent Cloud's existing primitives. They are design suggestions, not verified facts.

1. **Attenuated sub-credentials with budgets.**
   - Extend the existing token-exchange endpoint (15-min tokens) to accept an RFC 8693-style request: `subject_token` is the parent agent's token; requested scope/paths must be a subset (already enforced). Add new constraint fields: `max_calls`, `max_bytes_written`, `max_spend`, `not_after`, `task_id`, `allowed_tools`.
   - Record the parent in an `act`-style chain claim.
   - Enforce budgets server-side, since Agent Cloud is the data plane. That avoids Biscuit's offline-verification complexity while matching its "can only narrow" semantics.
   - A Biscuit option is available later if offline or third-party verification is needed.
2. **Delegation-chain audit.**
   - Store each mint as an edge: parent cred -> child cred, with constraints, reason/task, minting time and revocation.
   - Expose "show me everything that descends from agent X / human Y", plus "revoke subtree".
   - No product I found exposes subtree revocation as a product feature.
3. **Owner-of-record plus cascading decay** (Entra's model, made self-hostable and chain-aware):
   - Every root agent credential has a human owner-of-record (required, like Entra sponsors) and a `reauth_every` cadence, e.g. 30/90 days.
   - On a missed re-auth, an owner deactivated in the linked IdP (SCIM/OIDC), or N days of inactivity: auto-pause the agent, which cascades to all descendants. Notify, then allow transfer to a named successor.
   - Pair with an unused-grant report per agent (granted paths/scopes vs. used in the last N days, à la AWS Access Analyzer). Add one-click "shrink to observed use".
4. **Kill switch with a measured window.**
   - A single call revokes the credential subtree, cancels in-flight tasks and blocks new token exchanges.
   - Because access tokens last 15 min, the worst-case post-kill window is bounded. An introspection/denylist check on the data plane can push it to near-zero. Publish the measured number.
5. **Secrets out of context.**
   - Resolve third-party secrets by reference at the data-plane/MCP boundary (Keycard-proxy style), so tool results never contain raw values.
   - Put this to an adversarial test. No vendor I found publishes evidence for its "never sees the token" claim.
6. **Interop, so it isn't an island.**
   - Publish RFC 9728 PRM and accept audience-bound tokens on Agent Cloud's MCP server.
   - Accept ID-JAG from enterprise IdPs for human-initiated root grants.
   - Emit `act` chains compatible with transaction-tokens-for-agents, so external services can read the chain.

### Gaps
- I did not verify Agent Cloud's code for the features listed in the brief, e.g. whether the 15-min exchange already records the parent or enforces anything beyond scope subset. Check `Hackingaround/agent-cloud` before building on these proposals.
- I found no market data on how much buyers value self-hosting for agent identity versus Entra/Auth0 SaaS.
