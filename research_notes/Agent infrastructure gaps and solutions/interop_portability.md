# Agent Interoperability and Whole-Agent Portability Standards (state as of Nov 2026)

Research date: 2026-11-22. About 17 tool calls (search + fetch). Primary sources were preferred. Anything not checked against a source is marked UNVERIFIED or listed under Gaps.

## Model Context Protocol: current spec, features, governance, registry, adoption

### Takeaway
The current MCP spec is **2026-07-28**. It made MCP a stateless request/response protocol, moved Tasks into an official extension, replaced server-initiated elicitation and sampling with Multi Round-Trip Requests, and deprecated Roots, Sampling and Logging. Governance is neutral: MCP was donated to the Linux Foundation's Agentic AI Foundation (AAIF) in Dec 2025, and AWS, Google, Microsoft and OpenAI are all platinum members. MCP standardizes *access* to tools and data. It does not standardize agent identity, memory or state.

### Cited Findings
**Spec versions and features**
- Latest release is 2026-07-28. The previous one was 2025-11-25. — [MCP blog, 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- 2026-07-28: "MCP is transforming from a bidirectional stateful protocol into a request/response stateless protocol". The `initialize`/`initialized` handshake and the `Mcp-Session-Id` header are retired (SEP-2575, SEP-2567). Each request carries its protocol version, client identity and capabilities in `_meta`. A new optional `server/discover` RPC is added. — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/); [changelog](https://modelcontextprotocol.io/specification/latest/changelog)
- Multi Round-Trip Requests (SEP-2322) replace server-initiated `elicitation/create`, `sampling/createMessage` and `roots/list`. The server returns `resultType: "input_required"` and the client retries with `inputResponses`. — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- Roots, Sampling and Logging are deprecated (SEP-2577), with at least 12 months of support. The HTTP+SSE transport is deprecated, and Streamable HTTP is the transport going forward. Streamable HTTP requests must carry `Mcp-Method` and `Mcp-Name` headers so gateways can route on them (SEP-2243). — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- List and read responses (`tools/list`, `prompts/list`, `resources/list`, `resources/read`) gain `ttlMs` and `cacheScope` (SEP-2549). — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- **Tasks:** these came in as experimental in 2025-11-25 ("durable requests with polling and deferred result retrieval"). In 2026-07-28 they moved into the `io.modelcontextprotocol/tasks` extension, which has poll-based `tasks/get`, a new `tasks/update` (SEP-2663) and a `subscriptions/listen` stream. — [2025-11-25 tasks spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks); [SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension.md); [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- A formal extensions framework now covers Tasks, MCP Apps and Enterprise Managed Authorization (EMA). — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- Authorization changes: RFC 9207 `iss` validation (SEP-2468), `application_type` in DCR so desktop and CLI apps can use localhost redirects (SEP-837), and client credentials bound to their issuer (SEP-2352). Dynamic Client Registration is deprecated in favour of Client ID Metadata Documents (CIMD). — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- Tier-1 SDKs (TypeScript, Python, Go, C#) support 2026-07-28. Rust support is in beta. — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

**Governance**
- The Linux Foundation formed the Agentic AI Foundation (AAIF) on **Dec 9, 2025**. Its founding projects were Anthropic's MCP, Block's goose and OpenAI's AGENTS.md. — [LF press release](https://linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation)
- Platinum members are AWS, Anthropic, Block, Bloomberg, Cloudflare, Google, Microsoft and OpenAI. The $350k-per-membership figure comes from secondary reporting. — [LF press release](https://linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation); [WorkOS](https://workos.com/blog/the-linux-foundation-launches-the-aaif)
- AAIF projects as of Aug 2026: MCP, AGENTS.md, goose, agentgateway, and A2A, which was added Aug 17, 2026. — [AAIF blog: A2A joins AAIF](https://aaif.io/blog/a2a-joins-aaif)
- The MCP site's copyright line reads "Model Context Protocol, a Series of LF Projects, LLC". The lead maintainers are David Soria Parra and Den Delimarsky. — [MCP blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)

**Registry**
- The official MCP Registry (modelcontextprotocol.io/registry) uses a standardized `server.json` with a JSON schema. Published version metadata is immutable. The About page still says the registry is "in preview" and that breaking changes or data resets may happen before GA. — [MCP Registry About](https://modelcontextprotocol.io/registry/about)
- Secondary report: the registry launched in preview on Sep 8, 2025, and its API froze at v0.1 on Oct 24, 2025. — [Atlan](https://atlan.com/know/ai-agent/what-is-an-mcp-registry/) (secondary, not checked against GitHub)

**Adoption**
- OpenAI: MCP support shipped in the Agents SDK in March 2025. ChatGPT desktop and the Responses API were promised at the same time. — [Constellation Research](https://www.constellationr.com/insights/news/openais-support-puts-mcp-pole-position-agentic-ai-standard); [DeepLearning.AI The Batch](https://www.deeplearning.ai/the-batch/openai-adopts-model-context-protocol-to-boost-llm-tool-integration)
- Google announced built-in MCP support in the Gemini API and SDK at I/O, May 2025. — [The New Stack](https://thenewstack.io/google-embraces-mcp)
- Microsoft announced MCP support in Windows, including a private registry with code signing. — [VentureBeat](https://venturebeat.com/ai/the-open-source-model-context-protocol-was-just-updated-heres-why-its-a-big-deal) (secondary)

### Inferences
- The stateless 2026-07-28 core makes it simpler to host Agent Cloud's MCP server behind ordinary HTTP load balancers and gateways. Header routing via `Mcp-Method`/`Mcp-Name` lets an API gateway enforce per-tool permissions without parsing request bodies, which suits Agent Cloud's permissions and audit-log design.
- The deprecations matter for Agent Cloud's MCP server. Anything built on Roots, Sampling, server-initiated elicitation or HTTP+SSE should move to Streamable HTTP plus MRTR. Approvals map naturally onto the `input_required` pattern: an approval is a pending input that the client resolves.
- The Tasks extension (`tasks/get`, `tasks/update`, `subscriptions/listen`) is the closest thing MCP has to "pending work". Agent Cloud could implement it so long-running operations survive client restarts. That is a persistence role, which MCP itself does not define.
- `ttlMs`/`cacheScope` on `resources/read` means resource caching is now spec-level. Versioned files exposed as MCP resources can advertise cacheability.

### Gaps
- I did not verify whether resource subscriptions (`resources/subscribe`) survive unchanged in 2026-07-28 under the stateless model. The blog mentions `subscriptions/listen` only in the Tasks context. Check the 2026-07-28 spec's resources page.
- MCP Registry GA status is unconfirmed. The official page still says preview.
- I did not check the current, late-2026 state of OpenAI and Microsoft MCP adoption, such as ChatGPT connectors or Copilot Studio. The citations above are 2025.
- MCP has no notion of agent identity beyond OAuth client identity and the `_meta` client info. I found no SEP on agent identity in this pass.

## Agent2Agent (A2A) and the wider protocol landscape (ACP, AGNTCY, ANP, AG-UI, NLWeb, llms.txt/agents.json)

### Takeaway
A2A reached **v1.0 in March 2026**. It is the agent-to-agent standard (Agent Cards, Tasks, Artifacts, streaming, push notifications). It moved from LF hosting (June 2025) into AAIF (Aug 2026), absorbed IBM's ACP (Aug 2025), and is integrated into Azure AI Foundry, Copilot Studio and Bedrock AgentCore. AGNTCY (Cisco, LF) covers discovery, identity and messaging infrastructure. AG-UI covers the agent-to-frontend layer. None of them define persistent agent state.

### Cited Findings
**A2A governance and adoption**
- Google donated A2A to the Linux Foundation on June 23, 2025, at Open Source Summit NA. — [Google Open Source Blog](https://opensource.googleblog.com/2026/04/a-year-of-open-collaboration-celebrating-the-anniversary-of-a2a.html)
- v1.0 was released in **March 2026** as "the first stable, fully production-ready version of the standard". No exact day is given. — [Google Open Source Blog](https://opensource.googleblog.com/2026/04/a-year-of-open-collaboration-celebrating-the-anniversary-of-a2a.html)
- LF one-year release (Apr 9, 2026): 150+ supporting organizations, up from 50+. 22k+ GitHub stars. SDKs in Python, JS, Java, Go and .NET. Integrated into **Azure AI Foundry and Copilot Studio** (Microsoft) and **Amazon Bedrock AgentCore Runtime** (AWS). The roadmap lists an interop spec, registry consolidation, testing tooling and security best practices. The Agent Payments Protocol (AP2) has 60+ supporting organizations. — [LF press release](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- The Google blog (Apr 2026) says "over 100" companies, while the LF says "150+". This is a minor discrepancy. — [Google blog](https://opensource.googleblog.com/2026/04/a-year-of-open-collaboration-celebrating-the-anniversary-of-a2a.html) vs [LF](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- A2A joined **AAIF as a hosted project** (announced Aug 17, 2026), alongside MCP, AGENTS.md, goose and agentgateway. — [AAIF blog](https://aaif.io/blog/a2a-joins-aaif)
- IBM's Agent Communication Protocol (ACP, BeeAI) merged into A2A in Aug 2025. The ACP team wound down development and contributes to A2A instead, and migration docs were published. — search summary of [LF AI & Data](https://lfaidata.foundation/?p=7689) (I did not fetch that page directly. The merge is widely reported but was only checked via the search snippet.)

**A2A v1.0 technical surface** (from [a2aproject/A2A whats-new-v1.md](https://raw.githubusercontent.com/a2aproject/A2A/main/docs/whats-new-v1.md))
- Agent Card: `supportedInterfaces[]`, where each entry has a URL, protocol binding and version. **Signed Agent Cards** use JWS (RFC 7515) over RFC 8785-canonicalized JSON. There is an authenticated extended card (`GetExtendedAgentCard`).
- Tasks: `GetTask` returns status, artifacts and optional history. The new `ListTasks` supports filtering and cursor pagination and is scoped to the caller. The states are submitted, working, completed, failed, canceled, rejected, input-required and auth-required, all with the `TASK_STATE_` prefix.
- Artifacts are made of `parts[]`. A unified `Part` holds one of `text`, `raw`, `url` or `data`. Tasks, messages and artifacts can carry `extensions[]`.
- Streaming: `SendStreamingMessage`, and `SubscribeToTask` to reconnect. Multiple concurrent streams receive the same ordered events.
- Push notifications: Create, Get, List and Delete `TaskPushNotificationConfig`.
- Transports: JSON-RPC, gRPC and HTTP+JSON. `a2a.proto` is normative.
- Auth: implicit and password flows removed. Device Code (RFC 8628) and `pkceRequired` added, and mTLS supported.
- **Multi-tenancy:** a `tenant` field on all requests, so one endpoint can serve multiple agents.
- The spec does not say how tasks or artifacts are stored or retained.

**Others**
- **AGNTCY:** open-sourced by Cisco in Mar 2025 and accepted into the Linux Foundation on Jul 29, 2025. Formative members are Cisco, Dell, Google Cloud, Oracle and Red Hat. Scope: discovery (Open Agent Schema Framework, OASF), identity, messaging (SLIM) and observability. It claims interop with A2A and MCP. — [LF press release](https://www.linuxfoundation.org/press/linux-foundation-welcomes-the-agntcy-project-to-standardize-open-multi-agent-system-infrastructure-and-break-down-ai-agent-silos); "75+ members" is from [Splunk](https://www.splunk.com/en_us/blog/artificial-intelligence/agntcy-structure-the-internet-of-agents.html) (secondary)
- AGNTCY's "Agent Connect Protocol" also abbreviates to ACP. It is a different thing from IBM's ACP. — [secondary landscape page](https://mikenguyen13.github.io/ai_in_action/522-agent-interoperability-landscape.html)
- **Agent Network Protocol (ANP):** a decentralized discovery and collaboration protocol built on DIDs, as described in a 2025 survey. — [arXiv 2505.02279](https://arxiv.org/pdf/2505.02279v1)
- **AG-UI** (CopilotKit, MIT): an event-based agent-to-frontend protocol. The client sends an HTTP POST and gets an SSE stream back. Events cover lifecycle, text messages, tool calls, state and interrupts. SDKs exist in TS, Python, .NET and Kotlin, with integrations for LangGraph, CrewAI and the Claude Agent SDK. — [ag-ui GitHub](https://github.com/ag-ui-protocol/ag-ui); [CopilotKit intro](https://www.copilotkit.ai/blog/introducing-ag-ui-the-protocol-where-agents-meet-users)
- **NLWeb** (Microsoft): a content-access layer that turns websites into natural-language interfaces for agents. — [secondary landscape page](https://mikenguyen13.github.io/ai_in_action/522-agent-interoperability-landscape.html) (not checked against Microsoft's own docs)

### Inferences
- A2A v1.0 has three features that map directly onto Agent Cloud. **Signed Agent Cards** give portable, verifiable identity. **`tenant` multi-tenancy** lets one Agent Cloud endpoint host many agents. **`ListTasks` plus artifacts plus push configs** amount to a persistence surface that someone has to store. Agent Cloud could serve A2A endpoints for its hosted agents: each agent gets an Agent Card signed with a key Agent Cloud holds, tasks and artifacts land in Agent Cloud's versioned store, and push notifications go out through its existing webhook system.
- Since both MCP and A2A now sit under AAIF with every major vendor as a member, a neutral home that speaks MCP and A2A natively is betting on the consensus stack, not a vendor's protocol.
- AGNTCY's OASF and identity work overlaps with Agent Card identity. Those two efforts compete or complement each other, and it is unclear which identity scheme wins.

### Gaps
- **agents.json and llms.txt:** I did not research these conventions in this pass. Their status and adoption are UNVERIFIED.
- I did not check whether ANP has any 2026 governance or adoption changes, or the current NLWeb status.
- A2A's exact v1.0 release day and the published spec URL (a2a-protocol.org/latest) were not fetched. The `announcing-1.0.md` path returned 404.
- AG-UI's governance (whether it is foundation-hosted) was not verified.

## Formats for agent definition, export and memory

### Takeaway
Three kinds of format exist, and none is a whole-agent standard. **Letta Agent File (.af)** serializes one Letta agent: memory blocks, message history, tools, tool rules and config. It is missing archival memory, files, MCP configs, multi-agent support and secrets. **Oracle Open Agent Spec** is declarative *definition* only (LLM config, prompts, tools, flows), with no runtime state. **AGENTS.md** is project instructions for coding agents. Consumer memory portability, such as Claude's import from ChatGPT, is copy-paste prompt-mediated text, not a file format.

### Cited Findings
- **Letta .af includes:** memory blocks ("in-context memory segments for personality, user info, etc."), complete message history with an `in_context` flag, tool definitions including source code and JSON schema, tool rules, the system prompt, model config and environment variables. — [letta-ai/agent-file README](https://raw.githubusercontent.com/letta-ai/agent-file/main/README.md)
- **Letta .af excludes or roadmaps:** archival memory ("We currently do not support Passages"), data sources and files, multi-agent `.af`, MCP server configs, schema migration, and cross-framework converters (unchecked on the roadmap). Secrets are exported as null. It warns that concepts like shared memory blocks "may need to be adapted per-framework". — [README](https://raw.githubusercontent.com/letta-ai/agent-file/main/README.md); [Letta docs](https://docs.letta.com/guides/agents/agent-file)
- Letta's import and export works across any Letta server, self-hosted or Letta API. — [Letta docs](https://docs.letta.com/guides/core-concepts/agent-file/index.md)
- **Oracle Open Agent Spec:** "a declarative, framework-agnostic configuration language" for agents and workflows (LLM settings, prompts, tools, flow structure), executable on compatible runtimes such as LangGraph, AutoGen and WayFlow. — [arXiv 2510.04173](https://arxiv.org/html/2510.04173v4); [oracle/agent-spec](https://github.com/oracle/agent-spec); [Oracle blog](https://blogs.oracle.com/ai-and-datascience/introducing-open-agent-specification)
- **AGENTS.md:** released by OpenAI in Aug 2025. It gives coding agents project-specific guidance and is adopted by 60,000+ open-source projects and tools (Codex, Cursor, Devin, Gemini CLI, Copilot, Jules, VS Code, among others). It is now an AAIF project. — [LF press release](https://linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation)
- **Claude memory import/export:** Anthropic's help center documents importing memory from ChatGPT and other providers, plus export for backup and migration. It is available on Free through Enterprise plans on web and Desktop. The flow is to paste a Claude-provided prompt into the other assistant and paste its output back. Launch coverage dates it to March 2026. — [Claude Help Center](https://support.claude.com/en/articles/12123587-importing-and-exporting-your-memory-from-chatgpt-and-other-ai-providers); [MacRumors](https://macrumors.com/2026/03/02/anthropic-memory-import-tool/)
- ChatGPT has a full data export (Settings → Data Controls → Export). Secondary sources say ChatGPT has no bulk memory-file upload. — [ClickUp guide](https://clickup.com/blog/move-chatgpt-memory-to-claude/) (secondary)

### Inferences
- The state of the art for "export my agent" is a vendor-specific JSON (.af) or a prompt-mediated text dump (consumer memory). Neither carries permissions, approval policies, audit history, files or pending work.
- Open Agent Spec covers the *charter/definition* layer, and .af covers the *memory/history* layer for a single runtime. An open bundle could reference Open Agent Spec for the definition and borrow .af's memory-block and message structure, instead of inventing both.

### Gaps
- I did not check whether OpenAI exposes a programmatic memory export API, or whether the OpenAI Assistants/Responses "conversations" objects are exportable in a standard way.
- I did not check Oracle Agent Spec's current version, its 2026 adoption, or whether it has moved to a foundation.
- I found no MCP or A2A SEP or proposal for agent-state export. This is absence of evidence from a limited search, not confirmation that none exists.

## Gaps in the standards and how A2A/MCP relate to persistent storage

### Takeaway
No standard I found covers exporting a whole agent (identity, charter, memory, files, permissions, history, pending tasks) and importing it elsewhere. MCP resources and A2A artifacts and tasks are *wire formats for access and exchange*. Both specs are silent on storage, retention and ownership. That leaves an open slot for a neutral persistence layer that both protocols point at.

### Cited Findings
- The A2A v1.0 spec defines `GetTask`/`ListTasks` and artifacts but does not describe how tasks are stored or persisted. — [whats-new-v1.md](https://raw.githubusercontent.com/a2aproject/A2A/main/docs/whats-new-v1.md)
- MCP Tasks offer "durable requests with polling and deferred result retrieval", but durability is up to the server implementer. — [2025-11-25 tasks spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks); [SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension.md)
- The LF positions A2A as agent-to-agent communication and MCP as tool and data connection, with the two complementary. — [LF press release](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- Letta .af is the closest whole-agent format, and it explicitly lacks files, archival memory, MCP configs and multi-agent support. — [README](https://raw.githubusercontent.com/letta-ai/agent-file/main/README.md)

### Inferences (proposed Agent Cloud moves; my synthesis, not sourced claims)
Coverage by layer:

| Layer | Existing standard | Covered? |
|---|---|---|
| Identity | A2A Signed Agent Card; AGNTCY identity; OAuth client (MCP) | Partial. Identity is discoverable, but there is no portable key or credential handoff |
| Charter/definition | Open Agent Spec; AGENTS.md; .af system prompt | Partial, and fragmented |
| Memory | .af memory blocks; consumer memory export (text) | Single-vendor only |
| Files | MCP resources (access only) | No export format |
| Permissions/approvals | MCP auth (OAuth scopes), EMA extension | Access-time only, no portable policy |
| History/audit | .af messages; A2A task history | No audit-log standard |
| Pending work | MCP Tasks extension; A2A tasks | Wire only, no handoff |

Concrete opportunities:
1. **Open "agent bundle" format.** A signed archive (manifest plus content-addressed blobs) containing: the Agent Card (identity, public keys), the charter (embed or reference Open Agent Spec / AGENTS.md), memory (adopt .af's memory-block and message JSON shapes, and possibly ship a `.af` importer and exporter for compatibility), versioned files with full history, the permission and approval policy, the audit log (hash-chained), and pending tasks (A2A Task objects and MCP task records with state). Secrets would be excluded or re-bound, following .af's null-secrets precedent. This covers every row in the table, and no existing standard does.
2. **Be the A2A host.** Serve an A2A v1.0 endpoint per hosted agent, using `tenant` to multiplex. Sign Agent Cards with keys Agent Cloud manages. Persist Tasks and Artifacts in the versioned file store. Implement push-notification configs on top of the existing webhooks. That makes Agent Cloud the persistence backend for A2A artifacts, and `ListTasks` history becomes durable and exportable.
3. **MCP resources as the read side of the same store.** Expose the agent's files, memory and audit log as MCP resources with `ttlMs`/`cacheScope`. Implement the `io.modelcontextprotocol/tasks` extension so long-running work survives restarts. Map approvals onto MRTR `input_required`, so an approval pending in Agent Cloud shows up to any MCP client as an input request.
4. **Neutrality argument.** MCP and A2A now both live in AAIF, with AWS, Google, Microsoft and OpenAI as members, so implementing both natively is the vendor-neutral position. A self-hosted, open-source store that holds the bundle is what makes "move the agent between models, frameworks and clouds" possible in practice. The protocols move messages, not the agent.
5. **Possible standards play.** Propose the bundle as an MCP extension (the extensions framework exists now) or as A2A `extensions[]`, rather than as a standalone format. This is speculative. I did not check how open AAIF is to new project proposals.

### Gaps
- I did not survey hosted competitors (Letta Cloud, OpenAI Agents/Assistants persistence, Bedrock AgentCore Memory, Azure Foundry agent service) for proprietary export capabilities. That is likely another researcher's scope, and it would sharpen how differentiated the bundle idea is.
- I did not verify whether any AAIF working group is already working on agent state or memory portability.
- I did not check how well MCP resource subscriptions work under the new stateless core, which matters for live file and memory sync.
