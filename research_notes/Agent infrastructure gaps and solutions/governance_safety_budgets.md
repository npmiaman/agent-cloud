# Governance and safety controls for autonomous agents on shared data (2025-2026)

Scope: scalable human approval, prompt-injection propagation through shared storage and memory, and per-agent cost and runaway controls. The goal is to find the gap for Agent Cloud. It already has per-grant approval policies (none/overwrite/all), proposals with diffs (including CSV column changes), stale-proposal detection, signed webhooks, an audit log, per-workspace storage quotas and per-credential rate limits.

Method note: 17 search/fetch calls. Items marked **[fetched]** were read in full this session. Items marked **[search snippet]** come from search-result summaries and were not opened. Items marked **[unverified, prior knowledge]** have no source checked this session and should be confirmed before they are quoted.

---

## 1. Scalable approval, policy-as-code and risk-based auto-approval

### Takeaway
Approval today comes in two kinds. The first is a binary per-tool gate: needs_approval / interrupt / decorator, as in the OpenAI Agents SDK, HumanLayer and the Microsoft Agent Framework. The second is a deterministic policy engine at the tool gateway, such as AWS AgentCore Policy (Cedar, GA March 2026). The only shipped risk-scoring auto-approver with published numbers is Anthropic's Claude Code auto mode, a two-stage classifier. It judges actions from the transcript, not from the content of a data change. Nobody found ships content-aware, risk-scored review of *data/file changes* (diff-level), batched approvals, or AI pre-review attached to a proposal. That gap lines up with what Agent Cloud's proposal/diff machinery already supports.

### Cited Findings

**Human-in-the-loop SDKs**
- OpenAI Agents SDK (JS and Python): a tool can set `needsApproval` / `needs_approval` to `true` or to an async function returning a boolean, so approval can be per call. On approval the run pauses and returns `interruptions` plus a resumable `RunState`. In Python, a callable approval rule that cannot safely inspect the arguments fails closed. `ShellTool`/`ApplyPatchTool` accept an `on_approval` handler for code-based decisions. Hosted MCP tools use `require_approval` with `on_approval_request`. Rejection messages can be customized. [search snippet] — [OpenAI Agents JS HITL](https://openai.github.io/openai-agents-js/guides/human-in-the-loop); [OpenAI Agents Python HITL](https://openai.github.io/openai-agents-python/human_in_the_loop); [OpenAI guardrails & approvals guide](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)
- Microsoft Agent Framework has a "tool approval" feature with similar naming. [search snippet, not opened] — [MS Agent Framework tool approval](https://learn.microsoft.com/mt-mt/agent-framework/agents/tools/tool-approval)
- HumanLayer (YC): an Apache-2.0 Python SDK whose decorator gates a tool function behind human approval through Slack or email. Email was in beta, and SMS/RCS and Discord were planned. PyPI versions run 0.5.x–0.7.9. The company also ships CodeLayer, an OSS IDE for coding agents, and reportedly an "Agent Control Plane" (ACP) Kubernetes scheduler. The ACP claim comes from a third-party source. [search snippet] — [PyPI humanlayer](https://pypi.org/project/humanlayer/); [YC launch](https://ycombinator.com/launches/M8e-humanlayer-human-in-the-loop-for-ai-agents-and-beyond); [YC company page](https://ycombinator.com/companies/humanlayer)
- "withHuman" is a separate product claiming approvals across Claude Code, Codex, LangGraph and Pydantic AI through an "open protocol (AAP)". Not verified beyond a Product Hunt listing. — [Product Hunt withHuman](https://www.producthunt.com/products/withhuman)

**Risk-based auto-approval (LLM-as-reviewer)**
- Anthropic Claude Code auto mode [fetched]: users accept **93%** of manual permission prompts. It has two layers:
  - (a) A server-side prompt-injection probe scans tool outputs and adds a warning when content looks like hijacking.
  - (b) A transcript classifier on Sonnet 4.6 runs before each action. Stage 1 is a fast single-token yes/no biased toward blocking. Stage 2 adds chain-of-thought and runs only on Stage 1 flags.
  - The classifier sees only user messages and tool calls. Assistant text and tool results are stripped ("reasoning-blind by design"). Safe built-in tools and in-project file edits skip the classifier.
  - After 3 consecutive denials or 20 in total, the session escalates to the human. Headless mode terminates instead.
  - Published rates: Stage 1 alone has 8.5% FPR on real traffic (n=10,000), 6.6% FNR on real overeager actions (n=52) and 1.8% FNR on synthetic exfiltration (n=1,000). The full pipeline has **0.4% FPR, 17% FNR (overeager) and 5.7% FNR (synthetic exfil)**.
  - Source: [Anthropic Engineering: Claude Code auto mode](https://anthropic.com/engineering/claude-code-auto-mode)
- Claude Code docs: auto mode is the default starting mode in v2.1.283+ for interactive terminal/VS Code sessions. Some actions are never auto-approved in any mode. [search snippet] — [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes)

**Policy engines applied to agents**
- **Policy in Amazon Bedrock AgentCore**:
  - Uses Cedar and enforces at the AgentCore Gateway, outside agent code. It intercepts every agent-to-tool call.
  - Default-deny once a policy is attached. The schema is auto-generated from the Gateway's MCP tool manifest.
  - Cedar Analysis checks the policy set. Partial evaluation hides always-denied tools from list-tools.
  - Natural-language-to-Cedar authoring.
  - GA **March 3, 2026**.
  - One page mentions a Cedar-compatible language "Dogwood". Unverified and possibly a doc artifact.
  - [search snippet] — [AgentCore Policy docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html); [GA announcement](https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available); [AWS Security Blog: why Cedar](https://aws.amazon.com/blogs/security/why-policy-in-amazon-bedrock-agentcore-chose-cedar-for-securing-agentic-workflows/); [Natural-language policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-natural-language.html)
- Invariant Labs "Guardrails" (now Snyk): a policy engine for contextual rules on agent behavior, such as restricting data flow, limiting tool access or flagging anomalous decisions. Snyk acquired Invariant on June 24, 2025. Whether Guardrails is still offered standalone is unclear. [search snippet] — [Snyk press release](https://snyk.io/news/snyk-acquires-invariant-labs-to-accelerate-agentic-ai-security-innovation); [SiliconANGLE](https://siliconangle.com/2025/06/24/snyk-acquires-invariant-labs-expand-ai-agent-security-capabilities/)
- OPA/Rego, Permit.io, Oso, Microsoft Copilot Studio approvals, LangGraph `interrupt()`: **[unverified, prior knowledge]**, not checked this session. Prior understanding: OPA and Oso are general authorization engines without agent-specific features beyond what users build. Permit.io has marketed agent/MCP authorization. LangGraph `interrupt()` pauses a graph node for human input. Copilot Studio supports approval steps in agent flows. Confirm before citing.

**Approval fatigue research**
- arXiv 2606.08919, "Oversight Has a Capacity: Calibrating Agent Guards to a Subjective, Fatiguing Human". It models reviewers as fatiguing with each escalation and argues the safety-optimal escalation rate is below "escalate everything". [search snippet] — [arXiv 2606.08919](https://arxiv.org/pdf/2606.08919)
- arXiv 2608.26626: a risks-and-controls framework for multi-agent systems across organisational boundaries. Not opened. — [arXiv 2608.26626](https://arxiv.org/pdf/2608.26626)
- Secondhand claims, **unverified**:
  - A study of 1,053 testers reportedly found human reviewers caught 13.6% of dangerous commands vs 89% for a Claude classifier.
  - A study of AI-agent PRs reportedly found approval rates rose while inline comments fell 22%.
  - Sources: [TechTarget](https://www.techtarget.com/it-strategy/news/366649960/The-human-in-the-loop-is-falling-asleep); [BankInfoSecurity](https://www.bankinfosecurity.com/ai-agents-make-human-oversight-less-reliable-a-32768)
- EU AI Act Article 14 requires human oversight for high-risk systems and names automation bias as a risk. [search snippet via TechTarget] — [TechTarget](https://www.techtarget.com/it-strategy/news/366649960/The-human-in-the-loop-is-falling-asleep)

### Inferences
- The industry consensus is to shrink the human queue with a classifier and escalate the residue. Anthropic's 93% approval rate and the fatigue paper both say that "approve all" policies, like Agent Cloud's `all`, degrade into rubber-stamping at volume.
- Existing gates are **per tool call**, not **per data change**. AgentCore Policy decides whether an agent may call a tool on a resource, but it does not see the content of the write. Claude Code's classifier sees the tool call (which includes edit content) but explicitly skips in-project file edits. Neither scores a diff by blast radius, for example "drops a CSV column used by 3 downstream agents" or "rewrites 80% of a file other agents read."
- Agent Cloud could differentiate on the following:
  - (1) A risk score per proposal from deterministic features: rows/columns touched, schema changes, deletes, file read-fanout, recency of other agents' reads, and whether the writer's inputs were tainted (ties to §2). An optional LLM pre-review summary would be attached.
  - (2) A policy tier with `auto` (low-risk auto-merge), `review`, `block`, plus Cedar-style declarative rules per grant rather than three fixed modes.
  - (3) Batching: group N similar proposals from one task into one approval with an aggregate diff.
  - (4) Escalation circuit breakers modeled on Claude Code's "3 consecutive / 20 total denials → stop".
  - (5) Approval-rate telemetry to detect fatigue, for example a reviewer approving more than 95% in under 5 seconds each.
- Cedar is a reasonable open engine to embed in a self-hosted product: Apache-licensed, analyzable, and AWS-backed. The licensing claim is prior knowledge and was not verified this session.

### Gaps
- Did not verify LangGraph interrupts, Copilot Studio approvals, Permit.io/Oso agent features, or OPA agent integrations this session.
- No primary source found for any product doing risk-scored approval of *file/data diffs*. This absence is from limited search (about 4 queries on this axis), not an exhaustive scan.
- The "Gotoh" agent-approval startup named in the brief was not found or searched. Treat it as unverified.
- The 1,053-tester and PR-study numbers are secondhand. The original papers were not located.

---

## 2. Prompt-injection propagation through shared storage and multi-agent memory

### Takeaway
The strongest defenses are architectural information-flow control: Google DeepMind's CaMeL and Microsoft's FIDES. They attach provenance/integrity labels to values *inside one agent run* and enforce policy deterministically. Labels are erased at the run boundary, so they do not survive being written to shared storage and read by another agent. Memory-poisoning attacks such as MINJA show that persistent stores are the propagation channel. OWASP lists this as ASI06 in its Agentic Top 10 (Dec 2025). A cluster of 2026 preprints (MemLineage, Agentic Reference Monitor, memory provenance laundering) proposes cross-session provenance, but none found is a product. No storage system found carries provenance/taint labels into what agents read. This is the clearest gap for Agent Cloud.

### Cited Findings

**Information-flow-control research**
- **CaMeL** (Google DeepMind, arXiv 2503.18813, March 2025):
  - Extracts control and data flow from the trusted user query so that untrusted data cannot change program flow. Plans run in a custom Python interpreter that tracks provenance.
  - Each value carries capability tags (who may read it, where it came from). Policies allow or deny tool calls based on those tags.
  - A quarantined LLM processes untrusted content and has no tool access.
  - Solves **77%** of AgentDojo tasks with provable security vs **84%** undefended. Code is public at google-research.
  - [search snippet] — [arXiv 2503.18813](https://arxiv.org/abs/2503.18813); [Simon Willison on CaMeL](https://simonwillison.net/2025/Apr/11/camel/)
- "Operationalizing CaMeL" (arXiv 2505.22852) covers enterprise hardening. Not opened. — [arXiv 2505.22852](https://arxiv.org/pdf/2505.22852)
- **FIDES** (Microsoft Research, arXiv 2505.23643; authors include Costa, Köpf, Russinovich, Tople):
  - A planner that tracks confidentiality and integrity labels, enforces policy deterministically, and adds primitives to selectively hide information from the LLM.
  - With policy checks on, it blocked all prompt-injection attacks in AgentDojo.
  - Ships as experimental in Microsoft Agent Framework (`agent-framework-core`, Python only). FIDES = "Flow Integrity Deterministic Enforcement System". Repo: microsoft/fides.
  - A "16% more tasks" figure comes only from an AlphaXiv summary and is unverified.
  - [search snippet] — [Microsoft Research FIDES](https://www.microsoft.com/en-us/research/publication/securing-ai-agents-with-information-flow-control/); [arXiv 2505.23643](https://arxiv.org/pdf/2505.23643); [MS Agent Framework security docs](https://learn.microsoft.com/lt-lt/agent-framework/agents/security)
- **MemLineage** (arXiv 2605.14421): notes that FIDES labels are erased at the run boundary. It cites "NeuroTaint" as propagating semantic taint across sessions but without cryptographic integrity on the label, and proposes lineage-guided enforcement for agent memory. [search snippet] — [arXiv 2605.14421](https://arxiv.org/pdf/2605.14421)
- **Agentic Reference Monitor** (arXiv 2604.04035): mediates every tool call against a provenance graph. Reports that flat provenance baselines miss "causality laundering" and transitive taint. [search snippet] — [arXiv 2604.04035](https://arxiv.org/html/2604.04035v1)
- **Memory Provenance Laundering** (arXiv 2607.29167): proposes a "non-amplification firewall", meaning provenance-preserving memory plus execution-time authority gating. [search snippet] — [arXiv 2607.29167](https://arxiv.org/pdf/2607.29167)
- Forged-reasoning attacks on agent memory (arXiv 2607.05029) list provenance labeling and taint-threshold filtering as lightweight memory-store defenses. [search snippet] — [HF paper page 2607.05029](https://huggingface.co/papers/2607.05029.md)
- arXiv 2604.16548 frames poisoned memory as persistent state contamination, distinct from one-shot injection. It reports a joint evaluation that bypassed 12 published prompt-injection defenses. [search snippet] — [arXiv 2604.16548](https://arxiv.org/html/2604.16548)
- "APPA: Recoverable Information-Flow Control" (arXiv 2607.24625) and a June 2026 survey on provenance-bearing memory (arXiv 2606.04990) were seen in results but not opened. — [APPA](https://www.alphaxiv.org/abs/2607.24625.md); [Survey 2606.04990](https://arxiv.org/html/2606.04990)

**Memory-poisoning attacks**
- **MINJA** (arXiv 2503.03704; NeurIPS 2025 / ICLR 2026 listings):
  - Injects malicious records into an agent's memory bank through normal queries only, with no privileged access, using bridging steps, an indication prompt and progressive shortening.
  - Reports **98.2%** injection success and **76.8%** attack success. These are earlier-version numbers, so recheck v5.
  - [search snippet] — [arXiv 2503.03704](https://arxiv.org/abs/2503.03704); [NeurIPS 2025 poster](https://neurips.cc/virtual/2025/poster/118152)
- AgentPoison (backdoor via poisoned memory/RAG; arXiv 2407.12784, NeurIPS 2024): **[unverified, prior knowledge]**.

**Standards**
- **OWASP Top 10 for Agentic Applications** (released Dec 2025, "2026" edition):
  - ASI06 is "Memory and Context Poisoning": poisoned memory, RAG or embeddings influence future behavior across sessions.
  - The OWASP launch post cites the Gemini memory attack.
  - Vendor mitigations: validate and classify data before it enters memory, and detect external access to RAG stores.
  - [search snippet] — [OWASP GenAI launch post](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/); [Palo Alto Networks](https://paloaltonetworks.com/blog/cloud-security/owasp-agentic-ai-security/)
- OWASP LLM Top 10 LLM01 (Prompt Injection): **[unverified, prior knowledge]**. Still the #1 item in the 2025 LLM list.

**Products and tooling**
- **Invariant Labs → Snyk** (June 24, 2025):
  - Invariant named "tool poisoning" (hidden instructions in MCP tool descriptions) on April 1, 2025, and "MCP rug pulls".
  - Its MCP-Scan scanner checks for tool-description injection, tool poisoning and cross-origin escalation, and reportedly now redirects to "Snyk Agent Scan".
  - Guardrails is its policy engine, now part of Snyk Labs / AI Trust Platform.
  - [search snippet] — [Snyk press release](https://snyk.io/news/snyk-acquires-invariant-labs-to-accelerate-agentic-ai-security-innovation); [tech.eu](https://tech.eu/2025/06/24/snyk-acquires-ai-security-pioneer-invariant-labs-for-a-new-era-in-ai-safeguards/)
- Claude Code auto mode ships a server-side prompt-injection probe on tool outputs that *annotates* suspicious content with a warning rather than blocking it. This is a production example of labeled delivery. [fetched] — [Anthropic Engineering](https://anthropic.com/engineering/claude-code-auto-mode)
- **[unverified, prior knowledge]**, not checked this session:
  - Microsoft Spotlighting (arXiv 2403.03172): delimiting, datamarking and base64-encoding untrusted input so the model can tell it apart from instructions.
  - Simon Willison's "dual LLM" pattern (2023): a privileged LLM never sees untrusted text, and a quarantined LLM handles it. CaMeL builds on this.
  - Meta LlamaFirewall / Prompt Guard 2: open classifiers for injection and jailbreak.
  - Lakera Guard: injection detection API. Lakera was reportedly acquired by Check Point in 2025.
  - Protect AI: reportedly acquired by Palo Alto Networks in 2025.
  - Prompt Security: reportedly acquired by SentinelOne in 2025.
  - C2PA content credentials: signed provenance manifests for media, not agent text.
  - OpenLineage: job/dataset lineage events for data pipelines, with no trust/taint semantics.
  - Verify all of these before citing.

### Inferences
- Detection classifiers (Lakera, Prompt Guard, the Claude Code probe) are probabilistic. 2604.16548 reports that 12 defenses were bypassed. The deterministic approaches (CaMeL, FIDES) need labels, and those labels die at the storage boundary. A shared workspace is therefore exactly where provenance gets laundered: agent A reads a poisoned web page, writes `summary.md`, and agent B reads `summary.md` as "workspace data".
- Agent Cloud owns the storage boundary and already versions files, so it is in a position nobody else is in. A concrete design:
  1. **Per-version provenance record**: writer principal (credential/agent/task), source class (`human`, `agent`, `external:web`, `external:email`, `upload`), the input file versions the writer read during the task, and a trust level.
  2. **Taint propagation**: a version written by a task inherits the minimum trust (maximum taint) of everything that task read in the same session. Read-sets come from the audit log keyed by task/session ID. This is FIDES-style integrity labels made persistent, which MemLineage says is missing.
  3. **Labeled delivery**: the API/MCP read response includes `trust`/`origin` fields, with an optional spotlighting/datamarking wrapper for low-trust content. FIDES/CaMeL-aware clients can ingest the labels directly.
  4. **Policy hooks**: a grant rule such as "writes derived from `external` content require approval", or "tainted content may not flow into files with `trusted` label X". This ties back into §1 risk scoring.
  5. **Quarantine and blast radius**: when a version is flagged, list every downstream derived version and the agents that read it (lineage query), then offer to revert or quarantine.
  6. **Signed labels**: HMAC/sign the provenance record. Agent Cloud already signs webhooks. This addresses MemLineage's critique that NeuroTaint has no integrity on its labels.
- Caveat: taint-by-read-set over-taints. Anything that touches web content becomes low-trust, which may push everything into review. It needs explicit "endorse/declassify" by a human approval, and FIDES/CaMeL have the same issue. Propagation also depends on agents reporting a task/session ID, which LiteLLM likewise requires (§3).

### Gaps
- Did not find any shipping storage product (object store, vector DB, agent memory service such as Mem0/Zep/Letta) that carries taint labels to readers. Those memory products were not specifically searched, so this is "not found", not "confirmed absent".
- Spotlighting, dual LLM, AgentPoison, LlamaFirewall, Lakera/Protect AI/Prompt Security status, C2PA and OpenLineage were not re-verified this session.
- The 2026 preprints were seen only as search snippets. Their claims and numbers were not read in full.

---

## 3. Per-agent cost budgets, runaway detection and kill switches

### Takeaway
LLM-spend budgets per key, team or user are mature in gateways: LiteLLM, Portkey (Enterprise), OpenRouter. LiteLLM now has per-*session* iteration and spend caps for agents. Runaway control in agent products is mostly a denial counter (Claude Code) or a call cap (LiteLLM `max_iterations`). Every one found is scoped to LLM tokens. None budgets storage writes, request volume, egress or approvals per agent or per task *across tools*, and none found does anomaly detection on agent behavior in a data store. Agent Cloud has per-workspace quotas and per-credential rate limits. The gap is per-task/per-agent budgets on non-LLM resources plus a kill switch.

### Cited Findings
- **LiteLLM virtual keys / budgets**:
  - Keys carry `max_budget` (USD) and `budget_duration` (e.g. "30d"). Keys without `team_id` get individual limits, and with `team_id` they draw on a team budget.
  - Budgets need a connected DB.
  - There are also temporary budget increases and per-model `model_max_budget` budget fallbacks (v1.92.x+).
  - Tag budgets exist.
  - [search snippet] — [LiteLLM Budgets, Rate Limits](https://docs.litellm.ai/docs/proxy/users); [Tag budgets](https://docs.litellm.ai/docs/proxy/tag_budgets); [Budget fallbacks](https://docs.litellm.ai/docs/proxy/budget_fallbacks); [Temporary budget increase](https://docs.litellm.ai/docs/proxy/temporary_budget_increase)
- **LiteLLM Agent Iteration Budgets** [fetched]:
  - `max_iterations` is a hard cap on LLM calls per session. `max_budget_per_session` is a USD cap per session.
  - The session is identified by the `x-litellm-trace-id` header or `metadata.session_id`. `require_trace_id_on_calls_by_agent` / `..._to_agent` return 400 if the ID is missing.
  - Exceeding a limit returns HTTP 429 with `"type": "budget_exceeded"`. Counters expire after 1 h by default (`LITELLM_MAX_ITERATIONS_TTL`, `LITELLM_MAX_BUDGET_PER_SESSION_TTL`).
  - The docs disagree on whether config lives in key metadata or the agent's `litellm_params`. No version is stated.
  - [LiteLLM Agent Iteration Budgets](https://docs.litellm.ai/docs/a2a_iteration_budgets)
- **Portkey**: budget limits are Enterprise-only (older docs also mention select Pro users). They are set per provider integration/virtual key, reset weekly, monthly or never, and cannot be edited by org members once set. Limits apply at "five independent levels". [search snippet] — [Portkey budget limits](https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits.md); [Portkey enforcing limits and budgets](https://portkey.ai/docs/guides/use-cases/enforcing-limits-and-budgets)
- **OpenRouter**: org-admin spend caps are checked in order on every paid request. Workspace budgets set via API return 403 when exceeded. Per-key credit limits fail with `openrouter_key_limit`, and remaining credit is available at `GET /api/v1/key`. [search snippet] — [OpenRouter workspace budgets](https://openrouter.ai/docs/guides/features/workspaces/workspace-budgets); [OpenRouter limits](https://openrouter.ai/docs/api-reference/limits.md); [OpenRouter spend controls](https://openrouter.ai/docs/guides/best-practices/spend-controls.md)
- **Runaway/kill-switch example**: Claude Code auto mode stops and escalates after 3 consecutive or 20 total classifier denials, and headless mode terminates. [fetched] — [Anthropic Engineering](https://anthropic.com/engineering/claude-code-auto-mode)
- agentgateway.dev has LLM virtual keys / cost controls on Kubernetes. Not opened. — [agentgateway virtual keys](https://agentgateway.dev/docs/kubernetes/2.2.x/llm/cost-controls/virtual-keys/)
- **[unverified, prior knowledge]**, not checked this session:
  - Helicone: rate limits/caching per user.
  - Cloudflare AI Gateway: caching, rate limiting, analytics. Whether it has hard spend caps is unknown, and the search returned nothing.
  - AWS Budgets + cost allocation tags: alerting, and budget actions that can apply IAM/SCP denies, but billing data lags by hours.

### Inferences
- The gateway pattern Agent Cloud can copy is LiteLLM's: a required task/session ID per call, counters per (agent, task), a hard 429 with a machine-readable `budget_exceeded`, and a TTL. Agent Cloud would apply it to its own resources:
  - bytes written per task
  - proposals per task
  - files touched
  - reads/egress bytes
  - webhook fan-out
  - approval requests per hour (protects reviewers too, see §1 fatigue)
- Runaway signals Agent Cloud can see that LLM gateways cannot:
  - rewrite loops: the same file overwritten more than N times per task, or oscillating content hashes
  - proposal churn: many proposals rejected or stale
  - read amplification
  - writes after repeated denials
- A kill switch should do three things: revoke or suspend the credential, freeze its pending proposals, and emit a signed webhook. A one-click "revert everything task X wrote" uses versioning plus the audit log.
- AWS billing-based budgets lag (prior knowledge, unverified), so they cannot stop a runaway in real time. Enforcement must be inline, as LiteLLM's is. Agent Cloud's inline rate limiter is the right place.

### Gaps
- Cloudflare AI Gateway spend features, Helicone and AWS Budgets were not verified.
- Found no product doing ML anomaly detection on agent behavior for data stores or tools. Search was limited, so treat this as "not found".
- Did not find documented kill-switch features in OpenAI Agents SDK, LangGraph or AgentCore beyond the above (not searched specifically).
