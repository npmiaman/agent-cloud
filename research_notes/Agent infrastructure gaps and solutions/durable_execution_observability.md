# Durable Execution and Long-Horizon Observability / Decision Provenance for AI Agents (2025-2026)

Researched 2026-11-22. About 20 web searches/fetches. Most product facts come from vendor docs or blogs, found through search-result summaries. The pages fetched directly were the OTel agent-spans spec and the EU AI Act Art. 12 and Art. 19 pages. Claims marked **[unverified]** could not be confirmed from a primary page in this pass.

## 1. Durable execution for agents: what exists, what it provides, what's missing for agents

### Takeaway
By late 2026, durable execution for agents is a crowded field. Temporal, DBOS, Restate, Inngest, Cloudflare, Microsoft, LangGraph and Trigger.dev all cover the basics: checkpoints/replay, durable sleeps (up to about a year), external-event or human waits, and some form of code versioning. Every one of them models the agent as a *workflow run* or *entity*. None of them stores that run next to the agent's files, versioned artifacts, permissions and audit trail. Timers and waits fire on time or on named events, not on "this file/data changed". That gap is where Agent Cloud fits.

### Cited Findings

**Temporal**
- The OpenAI Agents SDK integration launched in public preview for Python on 2025-07-30. Python went GA on 2026-03-23, and Temporal's GA post also lists a TypeScript integration. — [Temporal blog](https://temporal.io/blog/announcing-openai-agents-sdk-integration); [BusinessWire](https://www.businesswire.com/news/home/20250730783559/en/Temporal-and-OpenAI-Launch-Integration-for-Enterprises-Developing-Production-Agents)
- How it works: a plugin redirects `Runner.run` so that each model call becomes a Temporal Activity, and agent code stays ordinary OpenAI Agents SDK code. It requires Python 3.10+ and Temporal Python SDK 1.33.0+. — [Temporal docs: OpenAI Agents SDK integration](https://docs.temporal.io/develop/python/integrations/openai-agents-sdk)
- Stated limit: Temporal's durability does not extend to MCP servers, which run independently of the Workflow. — [Temporal docs](https://docs.temporal.io/develop/python/integrations/openai-agents-sdk) (via search summary)
- Temporal also publishes generic "AI framework integrations" guidance, and a post on "agentic sandboxes" with the OpenAI Agents SDK. — [Temporal blog: AI framework integrations](https://temporal.io/blog/ai-framework-integrations); [Temporal blog: agentic sandboxes](https://temporal.io/blog/introducing-temporal-and-agentic-sandboxes-openai-agents-sdk)
- Versioning:
  - Worker Versioning went GA on 2026-03-30. Upgrade-on-Continue-as-New entered public preview at the same time.
  - **Pinned** workflows finish on one Worker Deployment Version. **Auto-Upgrade** workflows move to new code and must be kept replay-safe by hand with patching.
  - For "Long (weeks to years)" workflows, Temporal recommends PINNED + Upgrade on Continue-as-New.
  - [Temporal GA blog](https://temporal.io/blog/ga-worker-versioning-public-preview-upgrade-on-continue-as-new); [Temporal docs: Upgrade on Continue-as-New](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning/upgrade-on-continue-as-new)
- Temporal documents running Serverless Workers on Bedrock AgentCore Runtime. — [Temporal docs](https://docs.temporal.io/serverless-workers/agentcore)
- Durable timers, signals/updates and Activity retries are long-standing Temporal primitives. **[unverified in this pass; well-established from Temporal docs before 2025]**

**Restate**
- Virtual Objects are durable functions with an identity and transactional state. Restate guarantees one instance per key and queues calls. For sessions, one write handler runs per key at a time, which amounts to a per-key singleton/lease. — [Restate docs: sessions](https://docs.restate.dev/ai/patterns/sessions); [Restate blog](https://restate.dev/blog/durable-ai-loops-fault-tolerance-across-frameworks-and-without-handcuffs)
- Agents suspend on an awaitable while waiting for human input or a long LLM call, and resume when a signal arrives. — [Restate Aient case study](https://restate.dev/use-cases/aient)
- Integrations: Vercel AI SDK (2025-11-13), which logs and persists LLM calls, tool executions and session state changes; Pydantic AI (2026-05-12). — [Restate blog: Vercel](https://restate.dev/blog/building-durable-agents-with-vercel-and-restate); [Pydantic blog](https://pydantic.dev/articles/restate-durable-execution-pydanticai)
- All Restate claims are vendor statements. No independent benchmarks were found.

**DBOS**
- DBOS is an open-source library. Postgres is its only required infrastructure, with no separate orchestration server. Each step is checkpointed to Postgres, and `launch()` recovers incomplete workflows. A workflow goes to ERROR after a maximum number of recovery attempts. The paid Conductor control plane is recommended for HA recovery. — [DBOS architecture](https://docs.dbos.dev/architecture); [DBOS blog: workflows as Postgres rows](https://dbos.dev/blog/why-workflows-should-be-postgres-rows)
- Versioning:
  - Workflows are tagged with an application version, which defaults to a hash of the workflow source. Recovery only runs on matching versions.
  - `DBOS.patch()` (around March 2026) lets in-flight workflows keep the old path while new ones take the new path.
  - Reordering steps breaks recovery.
  - [DBOS upgrading workflows (Python)](https://docs.dbos.dev/python/tutorials/upgrading-workflows); [DBOS March 2026 features](https://dbos.dev/blog/dbos-new-features-march-2026)
- Relevance to Agent Cloud: DBOS is the closest architectural match for a self-hosted Postgres-backed product, because it can be embedded rather than run as a separate cluster. (Inference.)

**Inngest**
- Sleeps can last up to one year. The whole run, including sleeps, must fit within the plan's maximum run length: 30 days Free, 90 days Pro, 366 days Business, custom on Enterprise. A single step can run at most 2 hours. Sleeping runs use no compute and don't count toward concurrency. — [Inngest docs: step.sleep](https://www.inngest.com/docs/durable-execution/primitives/step-sleep); [Inngest: sleeps](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/sleeps)
- `step.waitForEvent` pauses until a matching event arrives or a timeout passes. An unofficial guide says the maximum timeout is 1 year and that only events sent *after* the step starts are caught. **[unverified]** — [Inngest Go step pkg](https://pkg.go.dev/github.com/inngest/inngestgo/step); [third-party guide](https://www.boringdocs.dev/docs/inngest-and-event-driven-workflow/step-coordination-waiting-for-external-events)
- AgentKit: not researched in this pass. **[gap]**

**Cloudflare (Workflows, Durable Objects, Agents SDK)**
- Workflows: `step.sleep` maximum is 365 days on both Free and Paid. Sleeping or waiting instances (sleep, retry, `waitForEvent`) don't count toward concurrency. Sleep steps don't count toward the step limit. Sources give conflicting step limits (10,000 default / 25,000 configurable vs. 1,024). — [Cloudflare Workflows limits](https://developers.cloudflare.com/workflows/reference/limits)
- `waitForEvent` timeout (24h default, 365d maximum) comes only from third-party notes. **[unverified]**
- Agents SDK:
  - State, per-agent SQLite data, schedules and "fiber checkpoints" survive hibernation and restarts. In-memory variables, setTimeout and open fetches do not.
  - `this.schedule` takes a delay, a Date or a cron string. It is backed by Durable Object alarms, and tasks are stored in SQLite.
  - [Cloudflare: long-running agents](https://developers.cloudflare.com/agents/concepts/long-running-agents/); [Cloudflare: schedule tasks](https://developers.cloudflare.com/agents/api-reference/schedule-tasks)
- Cloudflare is the strongest "cheap hibernation, one stateful object per agent" model. It is not self-hostable as a managed platform (workerd is open source, but DO hosting is Cloudflare's). **[self-host limitation is inference]**

**AWS (Bedrock AgentCore Runtime, Step Functions)**
- AgentCore Runtime:
  - `maxLifetime` defaults to 8 hours, and microVM runtimes accept at most 28,800 s.
  - Runtimes with a capacity provider (Instances) accept up to 1,209,600 s (14 days).
  - Idle timeout defaults to 15 minutes.
  - The docs disagree on whether a session ends at maximum lifetime or carries on in a new instance.
  - [AWS: lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html); [AWS: how it works](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html); [AWS quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
- AgentCore is a session runtime, not durable execution. Multi-day agents need external state and orchestration, for example Temporal on AgentCore. (Inference from the above.)
- Step Functions Standard workflows run for up to 1 year and support callback (task-token) waits. **[unverified in this pass; from prior knowledge of AWS docs]**

**LangGraph / LangSmith Deployment (formerly LangGraph Platform)**
- With a checkpointer, durable execution is on. There are three durability modes, from least to most durable: `exit`, `async`, `sync`. Production uses `PostgresSaver` (`langgraph-checkpoint-postgres`). `thread_id` is required for resume and interrupts. — [LangChain docs: durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution); [LangChain docs: checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- Sources conflict on which durability mode is the default. — [LangChain KB](https://kb.langchain.com/articles/6253531756-understanding-checkpointers-databases-api-memory-and-ttl) vs. a Mintlify mirror.
- Human-in-the-loop uses `interrupt`, and checkpoints allow time-travel/fork. **[unverified in this pass; standard LangGraph features]**
- LangGraph has no built-in long-duration durable timers. A scheduled wake-up needs cron jobs in the hosted platform or an external scheduler. **[unverified; not found in this pass]**

**Microsoft (Durable Task extension for Agent Framework)**
- Public preview was announced November 2025, and it is still preview in packages (`Microsoft.Agents.AI.DurableTask` 1.16.0-preview; Python `agent-framework-durabletask`, `agent-framework-azurefunctions`).
- Agents are implemented as durable entities. Multi-agent orchestrations checkpoint each agent call, and completed calls aren't re-run on recovery.
- Hosting options are Azure Functions or bring-your-own compute/Kubernetes.
- [Microsoft Learn: durable agents](https://learn.microsoft.com/azure/durable-task/sdks/durable-agents-microsoft-agent-framework); [Tech Community launch post](https://techcommunity.microsoft.com/blog/appsonazureblog/bulletproof-agents-with-the-durable-task-extension-for-microsoft-agent-framework/4467122); [NuGet](https://www.nuget.org/packages/microsoft.agents.ai.durabletask/)

**Trigger.dev**
- Waitpoint tokens:
  - `wait.createToken` takes a timeout and an idempotency key, and returns an id and a callback URL.
  - `wait.forToken` pauses the run.
  - A token is completed with the SDK (`wait.completeToken`) or with a POST to the callback URL, which needs no API key, so webhooks can complete it.
  - Statuses are WAITING, COMPLETED and TIMED_OUT.
  - [Trigger.dev: wait for token](https://trigger.dev/docs/wait-for-token); [Trigger.dev: create waitpoint](https://trigger.dev/docs/management/waitpoints/create)
- v4 introduced Run Engine 2 and pitched waitpoints for AI-agent human approval. — [Trigger.dev v4 beta](https://trigger.dev/blog/v4-beta-launch)

### Capability matrix (from the findings above; blank = not verified)
| Capability | Temporal | DBOS | Restate | Inngest | CF Workflows/Agents | LangGraph | MS Durable Task | Trigger.dev |
|---|---|---|---|---|---|---|---|---|
| Step checkpoint/replay | yes (Activities) | yes (Postgres) | yes (journal) | yes | yes | yes (checkpointer) | yes | yes |
| Long sleeps | yes [prior knowledge] | | | ≤1 yr, capped by plan run max | ≤365 d | no native timer [unverified] | | |
| External event / HITL wait | signals [prior] | | awaitables/signals | waitForEvent | waitForEvent | interrupt | | waitpoint tokens + HTTP callback |
| Code versioning mid-run | Worker Versioning GA + patching | version tag + patch() | | | | | | |
| Per-key singleton | per workflow ID [prior] | | Virtual Object per key | concurrency keys [unverified] | one DO per id | per thread | durable entity | |
| Self-hostable | yes (OSS server) | yes (library + Postgres) | yes (OSS server) | yes (OSS server) [unverified] | no (platform) | yes (OSS lib) | BYO compute (needs DTS/storage backend) | yes [unverified] |

### Inferences
- These products solve control-flow durability. Agents also need *context durability*: files, memory, charter/instructions, credentials and permissions that persist across months and survive a change of agent code or model. Every framework above keeps workflow state in its own store, separate from the agent's working files. Agent Cloud already holds versioned files, audit and idempotency, so the missing piece is a small durable-task layer *inside* the workspace, not a full Temporal replacement.
- Timers fire on time or on named events. None of the products verified here offers "wake the agent when file X or a query result changes" as a built-in. Users build it with webhooks plus `waitForEvent`/signals. Agent Cloud already has webhooks and a maintenance worker, so it could offer data-triggered wake-ups natively.
- Workflow versioning across months is still a sharp edge. Temporal and DBOS both need patching or pinning discipline. Agents make this worse because the "code" includes prompts, the model and tool schemas. A simpler agent-shaped model would treat each wake-up as a fresh, short run that rebuilds from durable workspace state plus a briefing, which Agent Cloud's "since your last session" already does. That largely sidesteps replay-versioning (similar in spirit to Temporal's Continue-as-New recommendation for long workflows).
- Cheap hibernation: Cloudflare DO and Inngest/Workflows sleeping instances use no compute. A self-hosted equivalent only needs a row in Postgres plus a due-time index, scanned by the existing maintenance worker.

### Gaps
- Inngest AgentKit's durability model, and the AWS Step Functions limits, were not verified in this pass.
- Restate's versioning story and maximum sleep duration were not checked.
- Official Cloudflare `waitForEvent` limits were not confirmed.
- The LangGraph default durability mode is contested between sources.
- Temporal's MCP durability limitation was seen only in a search summary of its docs.

## 2. Decision provenance and observability over long horizons

### Takeaway
LLM observability tools are built for debugging and evals over days to months, not for multi-year provenance. Hosted retention defaults are short: Datadog 15 d, Braintrust 14-30 d, LangSmith base 14 d, Arize AX 15-30 d. Long retention means paid tiers or self-hosting. Langfuse OSS and Phoenix keep data indefinitely by default. OTel GenAI agent/tool/MCP conventions exist but are all at "Development" status. They carry agent name/version and model, but they are not designed to link a span to a persistent artifact version. The EU AI Act requires lifetime-capable automatic logging for high-risk systems, kept for at least 6 months. The question "why did the agent change this file 6 months ago" remains unsolved in off-the-shelf tools.

### Cited Findings

**Retention (hosted defaults vs. self-host)**
- LangSmith:
  - Base tier keeps traces 14 days and extended keeps them 400 days.
  - Traces are auto-upgraded to extended when feedback is added, an automation rule matches, or the trace goes into an annotation queue. Adding a trace to a dataset does not upgrade it.
  - Enterprise can customize extended retention beyond 400 days.
  - A newer LangSmith page reportedly says that from **2026-09-14 the maximum long-lived SaaS retention is 180 days**, with earlier traces keeping their original retention. **[conflicting; seen via search summary, verify]**
  - Self-hosted TTL is configurable.
  - [LangSmith usage & billing](https://docs.langchain.com/langsmith/usage-and-billing); [LangChain KB: auto-upgrade](https://support.langchain.com/articles/3206725817-why-are-my-traces-being-automatically-upgraded-to-extended-retention); [LangSmith self-host TTL](https://docs.langchain.com/langsmith/self-host-ttl)
- Langfuse:
  - Cloud *data access windows* are Hobby 30 d, Core 90 d, Pro/Enterprise 3 years. The window limits viewing, not deletion.
  - Configurable retention (minimum 3 days) is available on Pro/Enterprise.
  - On self-host, retention is an Enterprise Edition feature. OSS stores data indefinitely by default, with no built-in TTL (per a GitHub discussion).
  - [Langfuse data retention](https://langfuse.com/docs/administration/data-retention); [Langfuse FAQ](https://langfuse.com/faq/all/data-access-vs-data-retention); [GH discussion #7460](https://github.com/orgs/langfuse/discussions/7460)
- Datadog LLM Observability:
  - Traces and spans are kept 15 days on all plans. An add-on extends this to 30/60/90 days (not on the free tier).
  - Annotated traces are kept 90 days.
  - Experiments are kept 15 d (free/on-demand) or 90 d (committed), with add-ons to 6/9/12 months.
  - [Datadog LLM Obs data governance](https://docs.datadoghq.com/llm_observability/data_governance/)
- Arize Phoenix (OSS) keeps data indefinitely by default (`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=0`). — [Phoenix docs: data retention](https://arize.com/docs/phoenix/settings/data-retention)
- Arize AX keeps data 15 d on Free and 30 d on Pro ($50/mo). **[third-party aggregator]** — [saastoolindex](https://saastoolindex.com/tools/arize-ax/)
- Braintrust keeps data 14 d on Starter and 30 d on Pro, extendable to 180 d at $0.50/GB/month. **[third-party, checked 2026-10-27]** — [evalsreview](https://evalsreview.com/tools/braintrust)
- Helicone: sources conflict (7 d vs. 1 month on free; up to "forever" by plan). One source says it is in maintenance mode. **[unverified]** — [dev.to pricing comparison](https://dev.to/shubhampalriwala/agent-observability-pricing-compared-which-tool-is-the-best-value-for-a-growing-team-2026-2fg0)
- W&B Weave: no retention figure found. **[gap]**

**OpenTelemetry GenAI semantic conventions**
- In v1.42.0 (2026-06-12) the GenAI conventions moved into a dedicated repo, `semantic-conventions-genai`. All `gen_ai.*` items are at **Development** status. — [Dash0 explainer](https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained); [spec repo](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md)
- Agent spans (fetched directly from the spec):
  - `create_agent` (CLIENT) carries `gen_ai.agent.id/name/version/description` (conditionally required) and `gen_ai.request.model`.
  - The internal invoke span uses `invoke_agent`, or `invoke_workflow` when the app defines control flow. It carries `gen_ai.conversation.id`, `gen_ai.data_source.id` and `gen_ai.request.model` (only when the agent uses a single model). Its token usage and finish reasons are Recommended.
  - `plan` spans exist.
  - `gen_ai.agent.version` is **not** listed on the invoke span.
  - Content attributes are **Opt-In** with `_ref` variants that point to external storage: `gen_ai.system_instructions`, `gen_ai.input/output.messages`, `gen_ai.tool.definitions`.
  - `gen_ai.conversation.id` must not be faked with a UUID or trace ID.
  - [OTel GenAI agent spans spec](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md)
- `execute_tool {tool.name}` spans carry `gen_ai.tool.name`, `gen_ai.tool.call.id` and `gen_ai.tool.type`. — [Dash0](https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained)
- MCP conventions (Development):
  - `mcp.method.name` is required (e.g. `tools/call`). The span is named `{method} {target}`, and server spans are kind SERVER.
  - `tools/call` spans also set `gen_ai.operation.name=execute_tool` and `gen_ai.tool.name`.
  - Trace context propagates in `params._meta.traceparent`.
  - MCP conventions are preferred over the RPC conventions. The MCP Python SDK and FastMCP implement them.
  - [OTel MCP semconv](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/); [FastMCP telemetry](https://gofastmcp.com/servers/telemetry); [MCP Python SDK OTel](https://py.sdk.modelcontextprotocol.io/run/opentelemetry/index.md)

**EU AI Act**
- Art. 12(1): "High-risk AI systems shall technically allow for the automatic recording of events (logs) over the lifetime of the system."
- Art. 12(2): logs must enable traceability for (a) identifying risk situations or substantial modifications, (b) post-market monitoring (Art. 72), and (c) monitoring operation (Art. 26(5)).
- Art. 12(3) adds minimum fields for Annex III 1(a) remote biometric ID systems: period of use, reference database, matched input, and the identity of human verifiers.
- [AI Act Art. 12](https://artificialintelligenceact.eu/article/12/)
- Application dates as shown on that page: 2 Dec 2027 for Annex III and 2 Aug 2028 for Annex I. This reflects the delay proposed in the Digital Omnibus. **[the page states these dates; the legislative status of the delay was not verified]** — [AI Act Art. 12](https://artificialintelligenceact.eu/article/12/)
- Art. 19(1): providers keep automatically generated logs "to the extent such logs are under their control", for a period appropriate to the purpose, "of at least six months, unless provided otherwise in the applicable Union or national law". Financial institutions keep them within their existing financial-services documentation. — [AI Act Art. 19](https://artificialintelligenceact.eu/article/19/)
- Art. 26(6) puts a similar at-least-6-month log retention duty on *deployers*. **[unverified in this pass; from prior knowledge of the text]**

### Inferences
- A 6-month legal floor already exceeds Datadog's default (15 d), Braintrust's (14-30 d) and LangSmith base (14 d). A multi-year "lifetime" posture is only reached by self-hosted Langfuse/Phoenix, paid Langfuse Pro (3 yr access window), or LangSmith Enterprise. That makes cheap long retention a real differentiator.
- Observability tools record *traces*, meaning what the model saw and said. They do not record *effects on durable state*, such as which file version an action produced, which charter version was in force, or which earlier artifact the decision relied on. OTel offers `gen_ai.agent.version`, `gen_ai.request.model`, `gen_ai.conversation.id` and `_ref` pointers, but nothing standard links a span to "file F, version 17". Answering "why did the agent change F six months ago" means joining the trace store with workspace history, and today that join is done by hand.
- Traces are typically deleted on a TTL, while files and versions persist. When the trace expires, the provenance link is lost. The fix is to keep a small, permanent *decision record* (ids, model, charter hash, input refs, a short rationale) next to each version, and let the bulky trace payload expire or move to cold storage.
- MCP propagates trace context in `params._meta.traceparent`. An MCP server like Agent Cloud's can read the caller's trace id on every tool call and stamp it onto the resulting version or audit row with no client changes beyond standard OTel MCP instrumentation.

### Gaps
- No primary source was found for W&B Weave retention or current Helicone retention. Braintrust and Arize AX figures come from aggregators.
- The LangSmith 180-day cap (Sept 2026) needs confirmation on the live page.
- No product was found that explicitly does drift detection over multi-month agent histories tied to model-version changes. Eval products (LangSmith, Braintrust, Phoenix, Datadog experiments) support datasets and experiments over time, but this was not researched in depth.
- The Art. 26(6) text and the AI Act delay's legal status were not verified.

## 3. How Agent Cloud could fill these gaps (integrate vs. build)

### Takeaway
Build a thin, agent-shaped durable layer and a permanent decision-record layer inside Agent Cloud, both on its existing Postgres, audit log, idempotency keys, maintenance worker and webhooks. Integrate outward with OTel export and inbound traceparent, plus an optional Temporal/DBOS bridge for teams that already run them. Don't try to rebuild a general workflow engine.

### Cited Findings (design anchors from the sources above)
- Postgres-row workflows with step checkpoints and version tags work at production scale ([DBOS](https://dbos.dev/blog/why-workflows-should-be-postgres-rows)). A self-hosted Agent Cloud can do the same without a new service.
- Sleeping work should use no compute ([Inngest](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/sleeps); [Cloudflare limits](https://developers.cloudflare.com/workflows/reference/limits)). Schedules persisted in the agent's own store, as Cloudflare does with SQLite and alarms ([Cloudflare Agents](https://developers.cloudflare.com/agents/concepts/long-running-agents/)), are the model to copy.
- For human or webhook waits, a token with an idempotency key, a timeout and an unauthenticated callback URL is the pattern to copy ([Trigger.dev waitpoints](https://trigger.dev/docs/wait-for-token)).
- For multi-year runs, restart into a fresh run instead of replaying across code changes ([Temporal GA versioning post](https://temporal.io/blog/ga-worker-versioning-public-preview-upgrade-on-continue-as-new)).
- Single-writer-per-key avoids races ([Restate sessions](https://docs.restate.dev/ai/patterns/sessions)).
- Use OTel attribute names for interop: `gen_ai.agent.id/version`, `gen_ai.request.model`, `gen_ai.conversation.id`, `*_ref`, and `mcp.method.name` with traceparent in `_meta` ([OTel agent spans](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md); [OTel MCP](https://opentelemetry.io/docs/specs/semconv/gen-ai/mcp/)).
- Minimum 6-month log retention for high-risk providers ([Art. 19](https://artificialintelligenceact.eu/article/19/)), and lifetime-capable logging ([Art. 12](https://artificialintelligenceact.eu/article/12/)).

### Inferences (concrete proposals; none of this exists in Agent Cloud yet unless noted)
1. **Durable task records, stored with the agent (build).** Add a table `tasks(id, agent_id, org_id, kind, state[pending|leased|waiting|done|failed], lease_owner, lease_expires_at, attempt, due_at, wait_token, wait_on, idempotency_key, charter_version, input_ref, result_ref, trace_id)`.
   - Workers claim tasks with `SELECT … FOR UPDATE SKIP LOCKED` and a lease TTL plus heartbeat. The maintenance worker reclaims expired leases.
   - Reuse the existing API idempotency keys for exactly-once side effects on Agent Cloud's own resources.
   - External side effects stay at-least-once unless the target supports idempotency. That is the same caveat Temporal and DBOS carry.
2. **Timers tied to data events (build; the main differentiator).**
   - `wait_on` accepts `{file_path, version>n}`, `{webhook_event}`, `{token}` or `{cron}`.
   - File-version writes and webhook ingests mark matching waiting tasks due, and the maintenance worker scans `due_at` for time-based tasks.
   - A waiting agent costs only a row.
   - On wake, it gets the existing "since your last session" briefing as its context. Each wake-up is a fresh short run, which avoids replay-versioning problems.
3. **Singleton per agent (build).** At most one active lease per `(agent_id, lane)`, like Restate's single writer per key. This stops two runs of the same agent from making conflicting edits.
4. **Human-in-the-loop waits (build).** Waitpoint tokens: create a token, return a signed callback URL, and complete it via the API, the MCP tool or a plain POST. Timeouts go into the same task table. Each completion is written to the audit log with a request id and the approver's identity, which also covers the Art. 12(3)(d)-style "who verified" field.
5. **Decision records linked to versions (build; small, permanent).**
   - On every file version or side-effecting call, store `decision(version_id | audit_id, trace_id, span_id, agent_id, agent_version, model_id (requested + response), charter_version_hash, tool_name, input_refs[file@version…], rationale_short, request_id)`.
   - Keep these rows forever. They are tiny. Trace payloads can expire.
   - This answers "why did the agent change F 6 months ago" with one join: version → decision → charter version + input versions + model + trace id (if the trace was retained).
6. **OTel in and out (integrate).**
   - Accept `traceparent` on the REST API and in MCP `params._meta`, then stamp it onto audit rows, versions and tasks.
   - Emit spans with `mcp.method.name`, `gen_ai.operation.name=execute_tool` and `gen_ai.agent.*` for Agent Cloud's own MCP tools.
   - Provide an OTLP exporter so users can send them to Langfuse, Phoenix, Datadog or LangSmith.
   - Don't build a trace UI. Link out by trace id.
7. **Cheap long retention (build, modest).**
   - Partition the audit log and decision records by month.
   - Move old partitions to compressed Parquet/JSONL on S3 or the local disk, with a hash chain per partition for tamper evidence.
   - Retention policy per org, defaulting to at least 6 months to match Art. 19.
   - Keep the live tail on hot partitions only.
8. **Bridges (integrate, optional).** Offer a DBOS or Temporal adapter that writes decision records and file versions through Agent Cloud's API using the workflow ID as the idempotency key. Teams already on those engines then get workspace provenance without moving their orchestration.
9. **Explicitly don't build:** a general-purpose replay engine, deterministic workflow code constraints, or an eval/drift dashboard. Instead, export decision records and traces to existing eval tools, and record `model_id` and `charter_version` so users can slice evals by them.

### Gaps
- Agent Cloud's current schema, worker and MCP code were not inspected for this note. The proposals assume only the features listed in the assignment (versions, audit with request ids and live tail, idempotency keys, webhooks, maintenance worker, MCP briefing).
- No measured cost data on long-retention storage was gathered.
- Whether an Agent Cloud-hosted agent would count as a "high-risk AI system" under Annex III depends on the use case and was not assessed.
