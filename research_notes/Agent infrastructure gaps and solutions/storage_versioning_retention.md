# Storage, Versioning, Coordination and Retention Layers for AI Agents (2025-2026)

Research date: 2026-11-22. Scope: (a) agent storage/filesystems, (b) git-for-data and multi-agent coordination, (c) retention, cost and GDPR erasure versus immutable history. Baseline for gap analysis, as stated by the caller and not re-checked against the code: Agent Cloud already has per-file immutable versions, content-addressed AES-GCM blobs, compare-and-swap writes, stable file ids across moves, soft delete plus a retention purge per workspace, backups and a replica.

Source quality note: most product facts come from vendor docs and blogs. Where a claim rests only on vendor marketing, a third-party blog or a preprint, it says so.

## Agent storage products: what they offer for multi-agent sharing, permissions and versioning

### Takeaway
"Agent filesystems" in 2025-2026 split into four groups: sandbox-local copy-on-write with audit (Turso AgentFS), POSIX volumes over object storage (Archil, Daytona/E2B/Modal volumes, Vercel Drives), MCP-fronted shared file stores with locks (Fast.io, LucidLink), and git-shaped versioned repos (Cloudflare Artifacts). None of the ones examined combines multi-agent shared workspaces, per-file versioning, leases, task branches with review/merge, and per-subject erasure in one self-hostable product. The frontier labs' own file features (Claude memory tool, OpenAI containers) are deliberately thin or ephemeral and push persistence back to the developer.

### Cited Findings
**Turso AgentFS (beta)**
- Uses an overlay for copy-on-write isolation: the host filesystem is a read-only base layer and a SQLite-backed AgentFS database is the writable delta layer. Writes go to the delta, the base is never modified, and deletions are recorded as whiteouts — [Turso AgentFS overlay guide](https://docs.turso.tech/agentfs/guides/overlay); [Turso blog](https://turso.tech/blog/agentfs-overlay)
- A 10GB project with 1MB of changes uses about 1MB in the delta layer — [Turso docs](https://docs.turso.tech/agentfs/guides/overlay)
- Linux uses FUSE plus namespaces; macOS uses NFS plus sandbox-exec (from a third-party write-up). Turso says only the transport differs — [codepointer Substack](https://codepointer.substack.com/p/agentfs-a-copy-on-write-filesystem); [Turso blog](https://turso.tech/blog/agentfs-overlay)
- Auditing: every file operation and tool call is recorded, queryable and exportable for compliance review. A `tool_calls` table is described as an insert-only log with parameters, results and timing (third-party) — [Turso auditing guide](https://docs.turso.tech/agentfs/guides/auditing); [codepointer Substack](https://codepointer.substack.com/p/agentfs-a-copy-on-write-filesystem)
- Docs mark it BETA. The third-party write-up notes that modifying a large base file triggers a full copy first — [Turso AgentFS intro](https://docs.turso.tech/agentfs/introduction); [codepointer](https://codepointer.substack.com/p/agentfs-a-copy-on-write-filesystem)

**Fast.io (MCP storage; vendor sources only)**
- Advisory per-file locks implemented as leases: acquiring returns a secret `lock_token`, `locked_at` and `expires_at`; a heartbeat renews it; a competing acquire gets HTTP 409; a crashed holder's lease expires or can be overridden. All writes are versioned and restorable — [Fast.io: file locks with the API](https://fast.io/resources/implement-file-locks-fastio-api/); [Fast.io: secure file locks](https://fast.io/resources/secure-file-locks-multi-agent/)
- Vendor pages contradict each other on which MCP tool exposes lock-acquire/release/status (`storage` vs `storage_manage`) and on the MCP endpoint URL — [Fast.io shared filesystem MCP](https://fast.io/resources/multi-agent-shared-filesystem-mcp/); [Fast.io file locks](https://fast.io/resources/implement-file-locks-fastio-api/)

**LucidLink MCP (public beta, June 2026)**
- MCP server that connects agents to a LucidLink filespace. Global file locking prevents conflicts when multiple agents write shared files. Works with Claude, OpenAI Agents SDK, LangChain, LlamaIndex and CrewAI — [Blocks & Files](https://www.blocksandfiles.com/file/2026/06/29/lucidlink-giving-ai-agents-file-access-with-mcp/5263643); [DBTA](https://www.dbta.com/Editorial/News-Flashes/LucidLink-MCP-Server-Targets-Agentic-AIs-Data-Layer-Gap-175504.aspx)
- CEO quote: "MCP is the protocol layer, not the persistence layer." — [Blocks & Files](https://www.blocksandfiles.com/file/2026/06/29/lucidlink-giving-ai-agents-file-access-with-mcp/5263643)
- The PyPI package `lucidlink-mcp` is proprietary and needs a service-account token. Its stated capabilities are read, write, search, lock, manage users and audit — [PyPI lucidlink-mcp 0.2.0](https://pypi.org/project/lucidlink-mcp/0.2.0/)

**Archil**
- Elastic POSIX filesystem in front of S3/R2/GCS buckets, which remain the durable store. Unused data is pushed down to object storage. The vendor claims 30x lower latency than S3 direct and petabyte-scale volumes — [Archil](https://archil.com); [Archil architecture docs](https://docs.archil.com/details/architecture)
- Integrations: E2B mounts Archil disks into sandboxes; Mastra ships `@mastra/archil` for persistent agent workspaces — [E2B docs: Archil](https://e2b.dev/docs/storage/archil); [Mastra blog](https://mastra.ai/blog/mastra-archil-persistent-storage)
- Pricing claims ($0.83 to $0.09 per GiB-month, "90% savings") are vendor marketing — [Archil pricing](https://archil.com/pricing)

**Sandbox persistence**
- E2B Volumes are in private beta (access via support). They live apart from the sandbox lifecycle and can be mounted to several sandboxes over time — [E2B volumes docs](https://e2b.dev/docs/volumes)
- Daytona: sandboxes persist by default. Volumes are S3-backed and attach to one sandbox at a time. GPU sandboxes are ephemeral — [Daytona persistence docs](https://www.daytona.io/docs/en/persistence)
- Vercel Sandbox: persistence is on by default (`persistent: false` opts out). Snapshots expire 30 days after last use by default. "Drives" give storage independent of the sandbox — [Vercel persistent sandboxes](https://vercel.com/docs/sandbox/concepts/persistent-sandboxes); [Vercel snapshots](https://vercel.com/docs/vercel-sandbox/concepts/snapshots); [Vercel drives](https://vercel.com/docs/sandbox/concepts/drives)
- Modal Volumes: "Last write wins in case of concurrent modification of the same file". Distributed file locking is not supported. Changes become visible to other containers only after commit/reload, and a reload fails while files are open — [Modal Volumes guide](https://www.modal.com/docs/guide/volumes); [modal.Volume reference](https://modal.com/docs/reference/modal.Volume)

**Cloudflare Artifacts (beta, April 2026)**
- Git-compatible versioned storage "for agents". Each repo maps to a Durable Object. Files sit in that DO's SQLite and are chunked around the 2MB row limit. R2 holds snapshots and KV holds auth tokens. The git engine is written in Zig and compiled to WASM. The pitch is "tens of millions of repos", fork from any remote, and a URL any git client can use — [Cloudflare blog](https://blog.cloudflare.com/artifacts-git-for-agents-beta/)
- Product page lists programmatic repo creation, branch, commit, diff and search. Pricing: $0.15 per 1,000 ops (first 10k/month free) and $0.50/GB-month storage — [Cloudflare Artifacts product page](https://www.cloudflare.com/product/artifacts/)

**Frontier-lab file features**
- Claude memory tool (`memory_20250818`) is client-side. Claude issues view/create/str_replace/insert/delete/rename commands against `/memories`, and the developer's handler maps them onto real storage and must validate paths against traversal. The SDK ships `BetaLocalFilesystemMemoryTool`. Platform docs say it is GA; an older docs page still shows a beta header — [Claude memory tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool); [older docs page](https://docs.claude.com/en/docs/agents-and-tools/tool-use/memory-tool)
- OpenAI Code Interpreter containers expire after 20 minutes of inactivity. OpenAI advises treating them as ephemeral and keeping data on your own systems. Generated files live in the container (container files API), not in `/v1/files`. Usage is billed per 20-minute increment from $0.03 for 1GB — [OpenAI Code Interpreter guide](https://developers.openai.com/api/docs/guides/tools-code-interpreter); [OpenAI community thread](https://community.openai.com/t/downloading-a-file-generated-by-code-interpereter-tool/1312629)

### Inferences
- Locks/leases with token, TTL and heartbeat are a recognized product feature (Fast.io, LucidLink). Agent Cloud's CAS writes give optimistic concurrency but no way to claim a file or a path prefix for the length of a task.
- Volume products (Modal, Daytona, Archil) target single-writer or last-write-wins use. Shared multi-agent editing with history is mostly unserved outside Fast.io/LucidLink, and both of those are proprietary SaaS. A self-hosted option is a real differentiator, though I did not verify whether Fast.io or LucidLink offer self-hosting.
- Since Claude's memory tool is "bring your own storage", Agent Cloud could ship a drop-in memory-tool handler backed by its versioned workspace. That would be a cheap integration with a clear story.

### Gaps
- Anthropic Files API and code-execution file handling: not checked this session.
- Whether AgentFS supports several agents sharing one delta, or merging deltas back to the base: not found.
- Fast.io's per-workspace permission model and retention settings: not fetched. LucidLink lock semantics (TTL? advisory or mandatory?): not documented in the sources found.
- No independent benchmarks for any of these products.

## Git-for-data, atomic multi-file commits, and agent branching/merge

### Takeaway
Branch → isolated writes → diff/review → atomic merge is now the standard pitch for agent data safety (lakeFS, Dolt, Cloudflare Artifacts, GitLake research). Coding agents get the same effect with git worktrees, which isolate files but not runtime and push conflicts to merge time. Object stores have no multi-object transactions of their own, so systems build atomicity from a metadata commit over immutable content-addressed objects, which is the architecture Agent Cloud already half-has.

### Cited Findings
- **lakeFS:** transactions are built from branches. A transaction branches from the source, runs operations, and merges back; rollback means not merging. Available in the Python SDK from 0.2.0 — [lakeFS Python transactions docs](https://docs.lakefs.io/reference/python/transactions/)
- lakeFS positions per-agent isolated branches with pre-merge hooks (schema/quality checks) and human approval before merge — [lakeFS blog: headless agents](https://lakefs.io/blog/headless-agents-are-coming-for-your-data/); [The New Stack](https://thenewstack.io/lakefs-agentic-ai-sandbox/)
- Search snippets report that lakeFS acquired the DVC project in late 2025 (secondary; I did not open a primary announcement) — [lakeFS blog search result](https://lakefs.io/blog/data-agents/)
- **Dolt:** Git's commit graph over a content-addressed Prolly Tree. Full git command set including rebase. Queryable diffs, and branch create/delete/rollback described as instantaneous (vendor) — [DoltHub: agentic branching](https://www.dolthub.com/blog/2025-06-18-agentic-branching); [DoltHub: Berkeley agents need branches](https://www.dolthub.com/blog/2025-09-24-berkeley-cs-agents-need-branches/)
- DoltHub argues PR-style review is essential for agentic systems, and in June 2026 rebranded from "Git for Data" to "the Database for Agents" — [DoltHub Oct 2025](https://www.dolthub.com/blog/2025-10-31-agentic-systems-need-version-control/); [DoltHub June 2026](https://dolthub.com/blog/2026-06-03-branch-bench-database-benchmarking-for-agentic-workflows)
- **GitLake** (Bauplan Labs, VLDB 2026 DASHSys workshop): lifts single-table Iceberg snapshots into lakehouse-wide commits, branches and merges so agents work on branches and humans review and publish (preprint/workshop) — [VLDB DASHSys paper](https://www.vldb.org/2026/Workshops/VLDB-Workshops-2026/DASHSys/DASHSys26_3.pdf); [HF papers](https://huggingface.co/papers/2607.08319.md)
- **Hugging Face Xet:** content-defined chunking at roughly 64KB, so only changed chunks upload. Replacing Git LFS, which stores full copies of each version (HF's example: one metadata line edited in a multi-GB GGUF). Migration ran through an LFS bridge with no cutover, moving "well over a dozen PBs". Native git client integration (git-xet) is "planned but under development" — [HF Xet docs](https://huggingface.co/docs/hub/xet/index); [HF: from files to chunks](https://huggingface.co/blog/from-files-to-chunks); [HF: migrating the Hub to Xet](https://huggingface.co/blog/migrating-the-hub-to-xet)
- **Coding agents and worktrees** (mostly third-party blogs, unverified): Claude Code has a `--worktree` flag and subagent `isolation: worktree`. Cursor's parallel agents use worktrees. Codex users script worktrees themselves. Worktrees isolate files but not ports, DBs or .env, and conflicts surface only at merge time — [Upsun dev blog](https://developer.upsun.com/posts/ai/git-worktrees-for-parallel-ai-coding-agents); [DEV: Claude Code worktrees](https://dev.to/jcamarate/claude-code-worktrees-parallel-agents-without-the-conflicts-3fnl); [MindStudio](https://mindstudio.ai/blog/git-worktrees-parallel-ai-coding-agents)

### Inferences
- Atomic multi-file commit on object storage is usually done the way lakeFS, Dolt, Artifacts and Iceberg do it: write immutable content-addressed blobs first, then atomically swing one pointer (a commit or branch head) in a transactional metadata store. Agent Cloud already has content-addressed blobs and CAS. A `commit` that CAS-updates a workspace/branch head referencing many file versions is a small step from there and does not require a new storage engine.
- File-level three-way merge is enough for an agent workspace: auto-merge when different files changed, flag a conflict when the same file id diverged. Agent Cloud's stable file ids across moves make rename-aware merge simpler than in git, which infers renames heuristically.
- Chunk-level dedup (Xet-style CDC) is the known fix for version-heavy storage growth on large, slightly edited files. Agent Cloud dedups whole blobs by content address, so every edit to a large file stores a full new copy. This is inference from the stated design; I did not check the code.

### Gaps
- Not verified this session: Project Nessie / Iceberg branch-and-tag semantics, Oxen.ai, DVC internals, Git LFS limits. No primary sources were fetched for these.
- Official Claude Code, Codex and Cursor docs on worktree isolation were not fetched. The claims rest on third-party blogs.
- No agent-specific merge tool was found that does semantic (not textual) conflict resolution in production.

## Coordination primitives: leases, locks, CRDTs, conflict resolution

### Takeaway
Shipping agent storage products use advisory leases (token, TTL, heartbeat, takeover) or mandatory global locks. Recent research finds CRDTs guarantee convergence but not correctness: explicit claim/lock tools carry most of the benefit, and semantic conflicts remain.

### Cited Findings
- Fast.io's lease design (token, `expires_at`, heartbeat renewal, 409 on contention, expiry/override for crashed holders) is a concrete, documented pattern (vendor) — [Fast.io](https://fast.io/resources/implement-file-locks-fastio-api/)
- LucidLink markets "global file locking" across multiple agents — [Blocks & Files](https://www.blocksandfiles.com/file/2026/06/29/lucidlink-giving-ai-agents-file-access-with-mcp/5263643)
- Modal explicitly offers no distributed locking and uses last-write-wins — [Modal Volumes guide](https://www.modal.com/docs/guide/volumes)
- CodeCRDT (arXiv 2510.18893): Yjs document as shared state, with agents claiming TODOs via optimistic write-verify. Across 600 trials: 100% convergence, zero merge failures, but 5-10% semantic conflicts, and results ranged from 21.1% faster to 39.4% slower — [arXiv 2510.18893](https://www.arxiv.org/pdf/2510.18893)
- AgentRoom (arXiv 2608.23740; listed at ICML/NeurIPS 2026): file-level claim/status/broadcast tools over a CRDT-merged filesystem. Removing the coordination tools while keeping the CRDT loses most of the gain — [arXiv 2608.23740](https://arxiv.org/pdf/2608.23740)
- S-Bus (arXiv 2605.17076): character-level CRDTs (Automerge, Yjs, Loro) suit additive state. Optimistic concurrency control is still needed where no merge function is well defined — [arXiv 2605.17076](https://arxiv.org/pdf/2605.17076)

### Inferences
- For Agent Cloud the evidence favors leases plus CAS plus branch/merge over adopting CRDTs. A lease API (acquire on a file id or path prefix, TTL, heartbeat, fencing token checked on CAS write) closes the gap with Fast.io/LucidLink. Fencing tokens make leases safe against paused holders. That is standard distributed-systems practice, not taken from a source above.
- CRDTs make sense only for specific shared artifacts such as a scratchpad or notes doc, not as the storage model.

### Gaps
- etcd leases and Redis-based locks (Redlock) were not fetched this session, so no primary citations are given here.
- The CRDT results are preprints. No production agent-storage product using CRDTs as its core was found.

## Retention, cost accumulation, and GDPR erasure versus immutable history and backups

### Takeaway
Regulators accept that backups need not be edited immediately, but they expect tracked, demonstrable erasure: re-apply erasures on restore, delete within defined retention, and do not exclude backups by default. The EDPB's February 2026 CEF report found half of supervisory authorities flagging backup erasure as a problem and promised more guidance. It does not mention crypto-shredding. Crypto-shredding (per-subject keys, destroy the key) is the standard engineering answer for immutable stores, but its legal sufficiency is debated. WORM features (S3 Object Lock compliance mode) directly conflict with erasure, and lifecycle rules are the cost lever for noncurrent versions.

### Cited Findings
**EDPB CEF 2025 report on the right to erasure (adopted 10 Feb 2026)**: text extracted from the official PDF.
- 32 SAs took part and 764 controllers responded — [EDPB press release](https://www.edpb.europa.eu/news/edpb-identifies-challenges-hindering-the-full-implementation-of-the-right-to-erasure_pt); [Reed Smith summary](https://www.reedsmith.com/our-insights/blogs/viewpoints/102mm9l/edpb-report-on-the-right-to-erasure-key-takeaways-from-the-2025-coordinated-enfo/)
- Issue 6 (backups), quoted: "Depending on the technical settings and risks, it might not always be advisable to modify or delete information from back-ups. But, in that case, organisations should have appropriate procedures to keep track of erasure requests and comply with them on restored systems" — [EDPB CEF report 2025, §4.2.6, p.20](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)
- "Half of the responding SAs raised concerns". Many controllers relied only on generic automatic deletion or backup retention periods, not request-specific procedures. Incremental deletion "spanning long periods of time" made it hard to show erasure happened "without undue delay". Some controllers "excluded back-up data by default, without providing a justification" — [EDPB CEF report §4.2.6 and §4.2](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)
- Recommendations: follow established standards for secure erasure, and "Verify that erasure has been carried out and be able to demonstrate such erasure." Best practices cited: overwrite the personal data with random strings, and move personal data automatically to employee-inaccessible stores, then delete or anonymise it. The EDPB "may consider" more guidance on backups and on "without undue delay" — [EDPB CEF report §4.2.6, p.21](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)
- Issue 7: controllers misuse "anonymisation" as a substitute for deletion. EDPB anonymisation guidelines are in preparation — [EDPB CEF report §4.2.7](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)
- Crypto-shredding / key deletion is not mentioned in the report. A grep of the extracted text found no "crypto" and no "encrypt" — [EDPB CEF report](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)

**UK ICO**
- ICO guidance treats backups pragmatically: data that cannot be overwritten immediately should be put "beyond use" until the normal backup cycle replaces it, and individuals should be told about delays (from search snippets; I did not fetch the live ICO wording). The guidance is under review after the Data (Use and Access) Act 2025 — [ICO right to erasure](https://ico.org.uk/for-organisations/guide-to-dp/guide-to-the-uk-gdpr/individual-rights/right-to-erasure)

**Crypto-shredding**
- Pattern: encrypt each subject's personal data under a per-subject key (often wrapped by a KEK held outside the store), keep the immutable log, and delete the key to "erase". Implemented in event-sourcing libraries (patchlevel, cqrs-es-crypto, Axon extension) — [patchlevel event-sourcing (DeepWiki)](https://deepwiki.com/patchlevel/event-sourcing/5.4-personal-data-and-crypto-shredding); [cqrs-es-crypto](https://docs.rs/cqrs-es-crypto); [Axon crypto-shredding extension](https://github.com/everest-engineering/axon-crypto-shredding-extension)
- Known hole: unencrypted snapshots, derived state and caches defeat it ("snapshots are not encrypted... crypto-shredding will not redact it") — [cqrs-es-crypto docs](https://docs.rs/cqrs-es-crypto)
- Legal debate: Mathias Verraes quotes a reply that under GDPR encrypted personal data is still personal data, and suggests the pattern fits business-sensitive data better than personal data — [Verraes: Throw away the key](https://verraes.net/2019/05/eventsourcing-patterns-throw-away-the-key/)

**S3 Object Lock and lifecycle (cost and immutability)**
- Object Lock requires versioning and protects individual object versions. In compliance mode a locked version "can't be overwritten or deleted by any user, including the root user". "The only way to delete an object under the compliance mode before its retention date expires is to delete the associated AWS account." Governance mode allows bypass with `s3:BypassGovernanceRetention` — [AWS S3 Object Lock docs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- Legal hold has no expiry, lasts until removed, and is independent of retention periods. Variable retention (event hold plus duration) starts the clock on a business event such as account closure — [AWS S3 Object Lock docs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- Lifecycle: `NoncurrentVersionTransition` moves older versions to cheaper classes, and `NoncurrentVersionExpiration` permanently deletes them. Both accept `NewerNoncurrentVersions` (keep N, cap 100). AWS's example rule transitions to GLACIER after 30 days, keeps 5 newer versions and expires after 365 noncurrent days — [AWS NoncurrentVersionExpiration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_NoncurrentVersionExpiration.html); [AWS NoncurrentVersionTransition](https://docs.aws.amazon.com/AmazonS3/latest/API/API_NoncurrentVersionTransition.html); [AWS Storage Blog](https://aws.amazon.com/blogs/storage/optimize-storage-costs-with-new-amazon-s3-lifecycle-filters-and-actions)
- Snapshot expiry by default exists in agent products too, for example Vercel Sandbox snapshots expire 30 days after last use — [Vercel snapshots](https://vercel.com/docs/vercel-sandbox/concepts/snapshots)

### Inferences
- **Content addressing complicates erasure.** If identical plaintext from two subjects or orgs dedups to one blob, deleting for one subject must not delete for the other, and the content hash itself can confirm the content to anyone who already holds the plaintext. Per-org (and optionally per-subject) encryption keys, with dedup scoped inside a key domain, fix both problems. This is reasoning from the stated Agent Cloud design; I did not check the code.
- **Crypto-shredding fits Agent Cloud's AES-GCM blobs naturally:** wrap each blob's DEK with a per-org or per-subject KEK, and shred by destroying the KEK in all key stores, including the replica and backups of the key store. Backups of ciphertext then become unreadable without being rewritten. That matches the EDPB's "beyond use / comply on restore" spirit, but the EDPB has not endorsed crypto-shredding. Agent Cloud should therefore pair it with (1) an erasure ledger re-applied on any restore (an explicit EDPB expectation), (2) hard deletion of blobs at the next retention purge, and (3) a verifiable erasure receipt ("demonstrate such erasure").
- **Tiered retention:** S3 lifecycle shows the knobs customers expect: keep the last N versions hot, move older versions to cold storage after X days, expire after Y days, legal-hold override, event-based retention start. Agent Cloud's single per-workspace purge window could grow into a policy object: `keep_versions`, `cold_after_days`, `expire_after_days`, `legal_hold`, `erasure_overrides_history`.
- **Legal hold vs erasure:** a legal hold must block the purge but not crypto-shredding for subjects outside the hold's scope. Compliance-mode WORM on the blob store would make Art. 17 erasure impossible short of key destruction. Use governance-mode-style holds only.

### Gaps
- No EDPB or ICO text explicitly blessing or rejecting crypto-shredding as Art. 17 erasure was found. This is the key legal gap and needs counsel.
- Current live ICO backup wording was not fetched; snippets only.
- No primary data was found on version-storage cost growth for agent workloads (for example GB per agent-day). Cost-tier prices other than Cloudflare Artifacts and vendor Archil claims were not collected.

## Gaps Agent Cloud could fill: concrete proposals

### Takeaway
The open space is a self-hosted workspace that combines (1) task branches with atomic multi-file commit, review and merge, (2) leases with fencing, (3) per-org/per-subject crypto-shredding plus an erasure ledger, and (4) policy-driven tiered retention with legal hold. Competitors each cover one or two of these: lakeFS/Dolt/Artifacts do branching, Fast.io/LucidLink do locks, AgentFS does CoW and audit, S3 does retention primitives. None of the sources reviewed shows one product doing all four for agent file workspaces.

### Cited Findings
- Branch → review → atomic merge as the agent-safety pattern: [lakeFS transactions](https://docs.lakefs.io/reference/python/transactions/); [DoltHub](https://www.dolthub.com/blog/2025-10-31-agentic-systems-need-version-control/); [Cloudflare Artifacts](https://blog.cloudflare.com/artifacts-git-for-agents-beta/)
- Lease semantics to match: [Fast.io](https://fast.io/resources/implement-file-locks-fastio-api/)
- Coordination tools matter more than CRDT merging: [AgentRoom](https://arxiv.org/pdf/2608.23740)
- Erasure must be tracked, demonstrable and re-applied on restore: [EDPB CEF report §4.2.6](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf)
- Retention knobs customers know: [AWS lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/API/API_NoncurrentVersionExpiration.html); [AWS Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- Audit log of every operation as a feature: [Turso AgentFS auditing](https://docs.turso.tech/agentfs/guides/auditing)

### Inferences
1. **Task branches:** `branch = (workspace_id, base_commit, head)`. Agents write file versions onto a branch, and `commit` CAS-swaps the branch head over a manifest {file_id → version_id}, which is atomic across many files. Merge does a three-way merge by file id (rename-safe thanks to stable ids) with auto-merge for disjoint files and a conflict list otherwise. Add a review state (open/approved/merged) and pre-merge hooks like lakeFS.
2. **Leases:** `acquire(file_id | path_prefix, ttl) → {lease_id, fencing_token, expires_at}`, plus heartbeat, release, and admin takeover. CAS writes carry the fencing token and are rejected if a newer token exists. Expose as MCP tools.
3. **Crypto-shredding:** per-org KEK (required) and optional per-subject KEK tags on files. Scope dedup per key domain. `erase(subject)` destroys the KEK, writes to an append-only erasure ledger, purges blobs at the next cycle, and emits a receipt. Restore-from-backup replays the ledger before serving.
4. **Tiered retention policy:** keep the last N versions hot, cold tier after X days, expire after Y days, legal hold (no expiry, scoped), and retention clocks starting on events such as workspace archive. Also surface per-workspace version-storage cost so users see accumulation.
5. **Optional:** a Claude memory-tool handler and an AgentFS-style per-operation audit export, both cheap wins given the existing design.

### Gaps
- Whether Agent Cloud's current schema supports a manifest/commit object, key hierarchy, or dedup scoping was not checked against the code in this research pass.
- Demand signal (do agent builders ask for self-hosted versus SaaS?) was not researched.
