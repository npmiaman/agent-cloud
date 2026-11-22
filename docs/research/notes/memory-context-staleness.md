# Long-term memory, context efficiency, model-migration continuity and knowledge staleness for long-running agents (2025-2026)

Research date: 2026-11-22. Scope: primary sources where possible. Items marked **[UNVERIFIED]** are from training knowledge or secondary sources and were not confirmed against a primary page in this pass. Agent Cloud baseline (from the brief, not checked against code): persistent versioned files, BM25 chunk search with line ranges, targeted edits and line-range reads, a "since your last session" briefing in the MCP handshake, audit log.

## Memory systems: what they store, how they consolidate, forget, and revise beliefs

### Takeaway
The field has split into three designs. (1) **File/block memory the agent edits itself**: Anthropic's memory tool, Letta memory blocks, Claude Code CLAUDE.md plus auto memory. (2) **LLM-extracted fact stores with ADD/UPDATE/DELETE**: Mem0, LangMem, ChatGPT saved memories. (3) **Temporal knowledge graphs that invalidate facts instead of deleting them**: Zep/Graphiti, and Mem0's graph variant. Only design 3 keeps history and provenance for belief revision. Design 1 is the most portable and the closest to Agent Cloud's file model, but it has no built-in notion of validity, contradiction or provenance.

### Cited Findings
**Anthropic memory tool (file-based)**
- Client-side tool: "Claude requests file operations, and your application runs them." The `/memories` path is a prefix that the handler maps onto real storage, such as a per-user directory or database keys. The tool type is `memory_20250818`. It is documented as stable on the Claude API, Bedrock, Google Cloud and Foundry, and needs no beta header. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Commands: `view` (directory listing 2 levels deep with sizes, or a file with 6-wide line numbers and an optional `view_range`), `create`, `str_replace` (errors on multiple occurrences), `insert`, `delete`, `rename`. `view` truncates the text of files longer than 16,000 characters, so expect ranged follow-up reads. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- The API auto-injects a system prompt: "ALWAYS VIEW YOUR MEMORY DIRECTORY BEFORE DOING ANYTHING ELSE… ASSUME INTERRUPTION: Your context window might be reset at any moment." — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Forgetting is left to the implementer: "Periodically delete memory files that haven't been accessed in a long time." Size caps and path-traversal validation are also the implementer's job. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Recommended "multisession software development pattern": an initializer session writes a progress log, a feature checklist and an init-script reference. Each later session reads them first and updates the progress log before ending. A feature is marked complete only after end-to-end verification. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Claude Code has two memory layers, both loaded at session start: CLAUDE.md (written by humans) and auto memory (notes Claude writes from corrections and preferences). They are treated as context, not enforced config. — [Claude Code docs: memory](https://code.claude.com/docs/en/memory). Claims that MEMORY.md acts as an index loaded into the system prompt, and that auto memory is disabled via `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, come from third-party write-ups **[UNVERIFIED]** — [finisky blog](https://finisky.github.io/en/claude-code-memory-management)

**Letta (formerly MemGPT)**
- Sleep-time agents shipped in Letta 0.7.0 (blog post dated 21 April 2025). Creating such an agent spawns a primary agent plus a sleep-time agent. The primary agent "is not provided with tools to edit its core memory" (the in-context memory blocks). Only the sleep-time agent edits those blocks, running asynchronously and continuously rewriting the "learned context". The post argues this keeps memory cleaner than MemGPT's incremental edits. The two agents can use different models, and run frequency is configurable. — [Letta blog: Sleep-time compute](https://www.letta.com/blog/sleep-time-compute)
- A sleep-time agent can also process an uploaded data source in the background and write findings into the primary agent's memory blocks. — [Letta blog: Sleep-time compute](https://www.letta.com/blog/sleep-time-compute)
- Memory tiers: core memory (in-context blocks), recall memory (conversation history search) and archival memory (vector store), as described in the post's tool list. — [Letta blog](https://www.letta.com/blog/sleep-time-compute)

**Mem0**
- Extraction: an LLM reads a rolling conversation summary, the last m=10 messages and the new message pair, and extracts candidate facts. Update: for each candidate it retrieves the top s=10 similar memories, then the LLM picks **ADD / UPDATE / DELETE / NOOP** via a tool call. Contradictions are resolved by **DELETE** (the old memory is physically removed). At answer time, a prompt tells the model to prefer the most recent memory. — [Mem0 paper (arXiv 2504.19413, HTML)](https://arxiv.org/html/2504.19413v1)
- Mem0g (graph variant) stores entity nodes and relationship triplets. An LLM resolver marks obsolete relationships **invalid rather than deleting them**, which preserves history for temporal reasoning. — [Mem0 paper](https://arxiv.org/html/2504.19413v1)
- OpenMemory MCP (launched May 2025): a local-first memory server (Docker + Postgres + Qdrant) shared across MCP clients such as Cursor, Claude Desktop, Windsurf and Cline. It has a UI, per-client pause/revoke, and "audit logs for every read or write". A hosted version is now offered too. — [Mem0 blog: Introducing OpenMemory MCP](https://mem0.ai/blog/introducing-openmemory-mcp); [Mem0 docs: OpenMemory](https://docs.mem0.ai/openmemory)

**Zep / Graphiti**
- Bi-temporal edges. Timeline T records when a fact held true (`t_valid`, `t_invalid`). Timeline T′ records when the system created or expired the record (`t′_created`, `t′_expired`). Relative dates are resolved against each message's `t_ref`. — [Zep paper (arXiv 2501.13956, HTML)](https://arxiv.org/html/2501.13956v1)
- Contradictions: a new edge is LLM-compared with semantically related edges. On a temporally overlapping contradiction, the old edge's `t_invalid` is set to the new edge's `t_valid`. New information wins by T′ order, and old records are retained. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Provenance: "episodes" (message, text or JSON) are the raw, non-lossy inputs. Episodic edges and bidirectional indices trace each fact back to its source episode. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Hierarchy: communities are built by label propagation (chosen over Leiden for incremental extension) and carry LLM summaries. Periodic full refreshes are still needed. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Retrieval: cosine search, BM25 (Neo4j Lucene) and breadth-first graph expansion, then reranking (RRF, MMR, episode-mentions, node distance, cross-encoder). — [Zep paper](https://arxiv.org/html/2501.13956v1)

**LangMem / LangGraph**
- The LangMem SDK models semantic memory (facts), episodic memory (past interactions kept as examples) and procedural memory (rules/behaviour, including prompt updates). Memory can be written "hot path" by the agent via tools or by a background manager that extracts and consolidates. It is built on LangGraph's BaseStore. — [LangChain blog: LangMem SDK launch](https://blog.langchain.com/langmem-sdk-launch); [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide). The exact API names (`create_memory_store_manager`) were not checked against the reference docs **[UNVERIFIED]**.

**OpenAI**
- ChatGPT has two memory sources. "Reference saved memories" are explicit items stored separately from chats, so deleting a chat doesn't remove them. "Reference chat history" draws on past chats, and its retained details "can change over time as ChatGPT decides what's most useful". Turning off chat history schedules derived info for deletion within 30 days. A newer "improved memory" keeps an auto-updating summary, and users can switch between it and legacy memory. Help-center pages are inconsistent across versions. — [OpenAI Help: How does reference saved memories work](https://help.openai.com/en/articles/11146739-how-does-reference-saved-memories-work); [OpenAI Help: Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)
- Agents SDK "Sessions": `SQLiteSession` (in-memory or file-backed) stores conversation history. `OpenAIResponsesCompactionSession` wraps an underlying session and calls Responses API `responses.compact` after each turn when `should_trigger_compaction` fires, or manually via `run_compaction()`. Compaction rewrites the stored history. — [OpenAI Agents SDK: Sessions](https://openai.github.io/openai-agents-python/sessions/); [SQLiteSession ref](https://openai.github.io/openai-agents-python/ref/memory/sqlite_session/). This is short-term conversation memory, not a long-term fact store.

**Cognee**
- Not researched in this pass. From training knowledge: an open-source "ECL" (extract-cognify-load) pipeline building a graph plus vector memory. **[UNVERIFIED]**

**Benchmarks**
- LongMemEval (ICLR 2025): 500 manually created questions testing information extraction, multi-session reasoning, temporal reasoning, **knowledge updates** and **abstention**. Commercial assistants and long-context LLMs show "substantial performance drops". The proposed fixes are session decomposition, fact-augmented key expansion and time-aware query expansion. — [LongMemEval arXiv 2410.10813](https://arxiv.org/abs/2410.10813v2)
- Zep on DMR: 94.8% vs MemGPT 93.4%. On LongMemEval: up to 18.5% accuracy improvement and 90% lower latency vs baseline. These are vendor-authored numbers. — [Zep arXiv abstract](https://arxiv.org/abs/2501.13956)
- LoCoMo overall LLM-judge score (Mem0 paper, Table 2): full-context 72.90%, Mem0g 68.44%, Mem0 66.88%, Zep 65.99%, LangMem 58.10%, OpenAI 52.90%. Temporal sub-score: Mem0g 58.13, Mem0 55.51, Zep 49.31, LangMem 23.43, OpenAI 21.71. Memory tokens per query: Mem0 1,764, Mem0g 3,616, Zep 3,911, OpenAI 4,437, full-context ~26,031. Total p95 latency: Mem0 1.44s vs full-context 17.1s. Storage per conversation: Mem0 ~7k tokens, Mem0g ~14k, Zep >600k. These are vendor-authored; Zep publicly disputed Mem0's Zep numbers **[UNVERIFIED, not re-checked this pass]**. — [Mem0 paper](https://arxiv.org/html/2504.19413v1)
- A 2026 arXiv paper titled "When Memory Becomes Authority: Benchmarking Authority Collapse at the Memory Consolidation Boundary" appeared in search. Title only, not read. — [arXiv 2608.01679](https://arxiv.org/pdf/2608.01679)

### Inferences
- **Full context still beats every memory system on LoCoMo accuracy (72.9%)**. Memory systems mainly buy latency and token cost, about 90% fewer tokens. Agent Cloud should not claim accuracy wins from memory alone. The win is cost, persistence across sessions and models, and auditability.
- Mem0's base DELETE loses history. Graphiti and Mem0g keep it. Agent Cloud already versions files, so "invalidate, don't delete" comes almost for free: a superseded fact is still in the previous file version.
- Letta's sleep-time split (the conversational agent can't edit core memory, a background agent curates it) is a clean pattern for a workspace. Agent Cloud could run a background curator over a session's audit log and diffs.
- The Anthropic memory tool's command set (view with ranges, str_replace, insert, create, delete, rename) is nearly identical to what Agent Cloud already exposes. Agent Cloud could serve as a drop-in remote backend for `memory_20250818` handlers, with versioning and audit that Anthropic's client-side reference does not provide.

### Gaps
- Didn't verify Letta's current (2026) memory-block API, Letta's filesystem/"memory as files" features, or whether sleep-time agents are still the default.
- Didn't verify the Zep vs Mem0 benchmark dispute or Zep's own LoCoMo re-run.
- Cognee, MemOS, A-MEM and other 2025-26 memory papers weren't researched.
- Didn't find how ChatGPT's "improved memory" resolves contradictions internally; it isn't publicly documented.

## Context engineering for long horizons: compaction, sleep-time compute, hierarchical summaries, reload costs

### Takeaway
Anthropic's guidance (Sep 2025) has become the reference: treat context as finite because of "context rot". Use compaction (summaries), tool-result clearing (lightest touch), structured note-taking outside the window, sub-agents returning 1-2k-token distillations, and just-in-time retrieval by reference (paths, queries, links). Sleep-time compute moves the cost of rebuilding state off the critical path (about 5x less test-time compute for equal accuracy in the paper).

### Cited Findings
- "Context, therefore, must be treated as a finite resource". Recall degrades gradually as context grows ("context rot"). — [Anthropic: Effective context engineering for AI agents (29 Sep 2025)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Compaction in Claude Code keeps architectural decisions, unresolved bugs and implementation details, and drops redundant tool output. Tune compaction prompts for recall first, then precision. "One of the safest lightest touch forms of compaction is tool result clearing." — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Sub-agents may use tens of thousands of tokens but return "a condensed, distilled summary", often 1,000-2,000 tokens. — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Just-in-time retrieval keeps lightweight references ("file paths, stored queries, web links"). Metadata such as folder hierarchy, naming and timestamps signals relevance. Claude Code is hybrid: CLAUDE.md loads up front, glob/grep fetch on demand. — [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Platform primitives: context editing clears specific tool results client-side, and compaction summarizes the whole conversation server-side near the limit. Anthropic recommends combining compaction with the memory tool, "memory preserves the information that must survive summarization." — [Claude docs: Memory tool (Using with compaction)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool); see also [Claude docs: Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction) and [Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) (linked from the memory doc, not fetched).
- OpenAI equivalent: Responses API `responses.compact`, wrapped by the Agents SDK `OpenAIResponsesCompactionSession`. — [OpenAI Agents SDK: Sessions](https://openai.github.io/openai-agents-python/sessions/)
- Sleep-time compute (arXiv 2504.13171): about 5x less test-time compute for the same accuracy on Stateful GSM-Symbolic and Stateful AIME. Up to 13% and 18% accuracy gains when scaling sleep-time compute. 2.5x lower average cost per query when several related queries share a context (Multi-Query GSM-Symbolic). Effectiveness correlates with how predictable the queries are. — [arXiv 2504.13171](https://arxiv.org/abs/2504.13171)
- Hierarchical summaries in practice: Graphiti community summaries (incremental label propagation plus periodic full refresh) — [Zep paper](https://arxiv.org/html/2501.13956v1). Mem0 keeps an asynchronously refreshed conversation summary as extraction context — [Mem0 paper](https://arxiv.org/html/2504.19413v1).
- Claude Code auto-compaction triggers at about 93-95% of the window per third-party sources. Sources conflict and this isn't official. **[UNVERIFIED]** — [ytrofr guide](https://ytrofr.github.io/claude-code-guide/docs/guide/42-session-memory-compaction.html)

### Inferences
- Reload cost: full-context replay costs about 26k tokens per query vs about 1.8-4.4k for extracted memory (LoCoMo numbers above). For a workspace, the analogue is: don't re-send files. Send a briefing (Agent Cloud's "since your last session") plus references (path, version, line range) that the agent can expand on demand. This matches Anthropic's just-in-time guidance exactly.
- Agent Cloud's line-range reads and BM25 chunk hits are the "lightweight references" primitive. What's missing is **precomputed, per-version derived views**, such as file summaries, directory digests and changed-since-version digests. A sleep-time job can build these at write time instead of the agent building them at read time. That is the sleep-time-compute argument applied to a file store: queries against a workspace are predictable ("what changed", "where is X"), which is exactly where the paper says sleep-time compute pays off.
- Compaction summaries are lossy and vendor-specific. Persisting them as versioned workspace files (e.g. `/sessions/<id>/compaction-<n>.md`) would make them inspectable and reusable across models. No surveyed product does this as a first-class feature (absence not exhaustively verified).

### Gaps
- No primary numbers found for per-session state-reload cost in production agents beyond the LoCoMo token tables.
- Didn't fetch Anthropic's "Effective harnesses for long-running agents" post or the compaction API page for exact trigger parameters.

## Model migration and identity continuity

### Takeaway
Vendors treat migration as a prompt-rewrite-plus-evals problem, not a memory problem. OpenAI ships a Prompt Optimizer and per-model upgrade guides, and recent guidance says to rebuild prompts from a minimal baseline rather than swap models drop-in. Memory portability exists only inside ecosystems (Letta Agent File across Letta servers; OpenMemory across MCP clients). Plain files (Anthropic's `/memories`, CLAUDE.md) are the de facto model-neutral format. No standard schema for cross-vendor memory or identity exists.

### Cited Findings
- OpenAI's GPT-5 Prompt Optimizer (Playground) migrates and improves prompts from other models. The practical guide's sequence is migrate, then optimize prompts, then steer, with results measured against your evals. — [OpenAI Cookbook: GPT-5 prompt migration with the prompt optimizer](https://developers.openai.com/cookbook/examples/gpt-5/prompt-optimization-cookbook); [OpenAI: A practical guide to building with GPT-5 (PDF)](https://cdn.openai.com/pdf/47c0215b-8976-4f60-8e13-d69c2ddbc15e/a-practical-guide-to-building-with-gpt-5.pdf)
- Per-model upgrade guides exist for GPT-5.4 and GPT-5.5. Reporting on the 5.5 guidance says not to treat it as a drop-in for 5.2/5.4; start from the smallest prompt that works and make the smallest changes that recover behaviour. — [OpenAI: Upgrading to GPT-5.5](https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p5); [OpenAI: Upgrading to GPT-5.4](https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p4); [The Decoder summary](https://the-decoder.com/openai-says-old-prompts-are-holding-gpt-5-5-back-and-developers-need-a-fresh-baseline/) (secondary)
- MLflow `optimize_prompts()` rewrites prompts when switching models, using existing outputs as training data. — [MLflow: Rewrite prompts](https://mlflow.org/docs/latest/genai/prompt-registry/rewrite-prompts/)
- Letta Agent File (.af), launched 2 April 2025: an open JSON format serializing an agent (memory, tools with source and schema, env vars). Secrets are nulled on export. Import/export works across self-hosted Letta and Letta Cloud. A third-party bridge to Mastra exists (mastra-af-letta). SDK method names vary by version. — [Letta docs: Agent File](https://docs.letta.com/guides/agents/agent-file); [GitHub letta-ai/agent-file](https://github.com/letta-ai/agent-file)
- OpenMemory MCP gives cross-client portability (store in Cursor, retrieve in Claude), but storage is Mem0's own schema in Postgres/Qdrant. — [Mem0 blog](https://mem0.ai/blog/introducing-openmemory-mcp)
- Anthropic's memory tool is model-agnostic at the storage layer because the developer owns the storage. It is listed for Claude 4 and later models, including 4.5 through 5.x. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Anthropic publishes a model-migration reference in its Claude API skill and docs. Not fetched this pass. **[UNVERIFIED for specifics]**

### Inferences
- Identity continuity across model swaps has three separable parts: (a) the instructions/"constitution" (system prompt, CLAUDE.md, skills), (b) accumulated memory, and (c) behavioural evals that prove (a)+(b) still produce the same behaviour on the new model. Vendors cover (a) with prompt optimizers and (c) with general eval advice. Nobody ties all three to one versioned artifact.
- Agent Cloud already versions files. It could add: a pinned "agent profile" (system prompt + memory snapshot + model id) per version; a regression eval set stored in the workspace; and a migration report that diffs outputs on old vs new model before switching. That is concrete and self-hostable.
- Plain Markdown/JSON files with documented front-matter (e.g. `valid_from`, `source`, `confidence`) are the most portable memory format. Vector embeddings are not portable across embedding models and must be treated as rebuildable derived views keyed by `(file_version, embedding_model)`.

### Gaps
- Found no cross-vendor memory interchange standard. The closest are .af (Letta-centric) and MCP as a transport (not a schema).
- Didn't verify whether ChatGPT offers a memory export in a structured format. Anthropic ships an "import-memory" flow in Claude apps (seen as a skill name in this environment, not documented here) **[UNVERIFIED]**.
- No primary source found on how any vendor versions memory schemas over time.

## World staleness for research agents: validity, archiving, citation provenance, re-verification

### Takeaway
Graphiti is the only surveyed memory system with first-class fact validity intervals (bi-temporal). Fighting link rot relies on mature but external tools: Internet Archive Save Page Now 2 (API, concurrency-limited), Perma.cc (hosted, API, quota-limited), ArchiveBox (self-hosted, WARC/HTML/PDF/PNG, alpha REST API). Nobody surveyed attaches archived snapshots or validity windows to agent research outputs automatically, and no memory product schedules re-verification of facts.

### Cited Findings
- Graphiti stores `t_valid`/`t_invalid` (world time) and `t′_created`/`t′_expired` (system time) on every edge, invalidates on contradiction, and keeps history. — [Zep paper](https://arxiv.org/html/2501.13956v1)
- LongMemEval explicitly tests "knowledge updates" and "abstention" (knowing you don't know), and proposes time-aware query expansion. — [LongMemEval](https://arxiv.org/abs/2410.10813v2)
- Save Page Now 2 API: capture requests support `capture_outlinks=1` (returns outlinks with job_ids for status polling) and `if_not_archived_within` (returns an existing recent capture instead of re-capturing; comma-separated values set separate thresholds for outlinks). Errors include hitting the "limit of concurrent active capture sessions". The official doc is a Google Doc (search snippet says last updated July 2026). — [SPN2 public API docs](https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA/mobilebasic); [ArchiveTeam wiki: SPN](https://wiki.archiveteam.org/index.php/Internet_Archive/Save_Page_Now)
- Reported SPN limits of 3 concurrent captures (anonymous) vs 5 (authenticated) and about 5k/day anonymous come from a user forum. **[UNVERIFIED]** — [Greasy Fork discussion](https://greasyfork.org/ru/discussions/development/214822-how-to-make-multiple-web-archive-org-save-requests-at-once)
- Perma.cc (Harvard Library Innovation Lab) has a developer API for creating archives (docs at perma.cc/docs/developer). A 2018 source says free accounts were capped at 10 links/month; current quotas weren't verified. — [Harvard LIL: Perma.cc](https://lil.law.harvard.edu/our-work/perma-cc/); [Perma blog 2018](https://archive.blogs.harvard.edu/perma/2018/02)
- Link-rot evidence cited by Perma: >70% of URLs in law journals (1999-2011) no longer point to the cited information, and a Hiberlink study found 75% of links weren't archived within two weeks of publication. Secondary summaries; originals not checked. — [Perma blog: So how bad a problem is link rot](https://archive.blogs.harvard.edu/perma/2019/02/14/so-how-bad-a-problem-is-link-rot)
- ArchiveBox: self-hosted (pip/Docker), outputs HTML, PNG, PDF, TXT, JSON, WARC and SQLite, with a CLI, REST API (alpha) and webhooks. — [PyPI: archivebox](https://pypi.org/project/archivebox/0.9.34rc63/)
- Deep-research citation provenance (OpenAI, Gemini, Perplexity): not researched this pass. Training knowledge says they return inline URL citations but don't archive the cited pages or record retrieval timestamps per claim. **[UNVERIFIED]**

### Inferences
- A research agent's output has a shelf life the agent can't see. The fix is per-claim provenance: `{claim, source_url, retrieved_at, content_hash, snapshot_ref, valid_from, valid_until/recheck_at}`. Graphiti's bi-temporal model is the right conceptual template, but it lives in a Neo4j graph, not next to the documents that cite it.
- Agent Cloud has the pieces for a self-hosted version: versioned files (snapshot storage), audit log (who asserted what, when) and BM25 (find claims). Adding (1) a `capture(url)` tool that stores a WARC/HTML+PDF snapshot as a versioned workspace file, optionally also pushing to SPN2 with `if_not_archived_within` to avoid duplicate captures; (2) a facts sidecar per research file with validity and provenance; and (3) a scheduler that re-fetches sources on `recheck_at`, diffs against the snapshot hash and raises "source changed/dead" items in the next session briefing, would close the staleness loop end to end.
- The "since your last session" briefing is a natural surface for **contradiction and staleness alerts**: "3 facts expired, 1 source 404s, 2 new writes contradict file X v12".

### Gaps
- Didn't verify the current OpenAI, Gemini or Perplexity deep-research citation formats, or whether any of them expose archived snapshots or retrieval timestamps.
- Didn't verify current Perma.cc API quotas/pricing or SPN2 official rate limits.
- Found no product that schedules re-verification of stored facts. Absence is not proven; I only searched memory products.

## Gaps Agent Cloud could fill (synthesis against the surveyed field)

### Takeaway
The open space is a **self-hosted, file-native, model-neutral memory layer with versioned derived views, facts carrying validity and provenance, contradiction surfacing, and archived sources**. Each piece exists somewhere (Graphiti's validity, Letta's sleep-time curation, Anthropic's file memory API, ArchiveBox's snapshots, OpenMemory's cross-client audit). No surveyed product combines them on a plain-files, versioned substrate.

### Cited Findings
- File-based memory with a standard command set and developer-owned storage is officially supported by Anthropic (`memory_20250818`), but expiration, size caps, security and versioning are left to the implementer. — [Claude docs: Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- Bi-temporal invalidation with episode provenance exists only in graph form (Graphiti). — [Zep paper](https://arxiv.org/html/2501.13956v1)
- Background curation of memory (sleep-time agents) is productized in Letta. — [Letta blog](https://www.letta.com/blog/sleep-time-compute)
- Cross-client memory with per-client revoke and read/write audit logs is productized in OpenMemory MCP. — [Mem0 blog](https://mem0.ai/blog/introducing-openmemory-mcp)
- Agent serialization for portability exists but is Letta-specific (.af). — [Letta docs: Agent File](https://docs.letta.com/guides/agents/agent-file)

### Inferences (concrete build list, ordered by leverage for Agent Cloud)
1. **Anthropic memory-tool-compatible backend.** Map `/memories/*` onto an Agent Cloud workspace path. The commands are already nearly 1:1 (view+range, str_replace, insert, create, delete, rename), and Agent Cloud adds versions and audit for free. Cheapest wedge, and works with every Claude model that supports the tool.
2. **Per-version derived views, maintained at write time** (sleep-time style): file summary, directory digest, BM25 chunks (already exist), optional embeddings keyed by `(file_id, version, embedding_model)` so a model or embedding swap triggers rebuilds rather than corruption. Serve summaries in the session briefing; expand by line range on demand.
3. **Facts sidecar with validity and provenance.** Front-matter or `.facts.jsonl` next to a document: `claim, subject, valid_from, valid_until, recheck_at, source_url, snapshot_ref, content_hash, asserted_by (session/agent/model), file_version`. Invalidate by setting `valid_until`, never delete (Graphiti semantics on files).
4. **Contradiction surfacing.** On write, BM25/embedding-retrieve related facts and run an LLM check (Mem0/Graphiti approach). Put conflicts in the next "since your last session" briefing instead of silently resolving them; the human or agent picks a winner, and the choice lands in the audit log.
5. **Source snapshots for research outputs.** A `capture(url)` tool stores a WARC/HTML/PDF via an ArchiveBox-style pipeline as versioned workspace files, plus optional SPN2/Perma.cc submission for a public permalink. Link each citation in a research file to its snapshot.
6. **Re-verification scheduler.** Re-fetch on `recheck_at`, compare hashes, flag changed or dead sources and expired facts in the briefing.
7. **Model-neutral agent profile and migration check.** Version the system prompt, memory snapshot and model id together. Keep a small eval set in the workspace and run it on old vs new model before switching, with a diff report. Export everything as plain files (optionally also as Letta .af) so nothing is locked to a vendor.

### Gaps
- None of the above was checked against Agent Cloud's actual code in this pass. Feature claims about Agent Cloud come from the brief.
- No user-demand evidence was gathered for which gap matters most; the ordering above is my judgement.
