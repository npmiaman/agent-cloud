# Agent Cloud Repository Format

**Version 0.1 (draft).** Status: experimental. Breaking changes are possible until 1.0.

An open format for storing the work of many AI agents directly on object storage (S3,
GCS, R2, MinIO or a local filesystem). It has branching, merging and lineage built in, and
no server in the write path. It plays the role for agent work that Iceberg plays for
tables: a layout of files plus a commit protocol, implemented by libraries that read and
write that layout.

The key words MUST, SHOULD and MAY are used as in RFC 2119.

## 1. Goals

1. **No coordinator.** Writers need only an object store with strong read-after-write
   consistency and conditional writes (`If-None-Match: *` and `If-Match: <etag>`).
2. **One writer per branch, by default.** Each agent commits to its own branch, so agents
   never contend. Work moves up a hierarchy (worker → team → project) through merges.
3. **Lineage is part of the format.** Every file version records what it was derived
   from. "Retract this result" finds everything downstream of it, across every agent and
   branch.
4. **Cheap at scale.** Content-defined chunking and prolly trees make the cost of a
   commit proportional to what changed, not to the size of the repository.
5. **Encrypted by default,** with keys held by the user's key management (KMS).

Non-goals: being a filesystem (no POSIX semantics) and real-time collaboration on a single
file.

## 2. Repository layout

A repository lives under a prefix `<root>/` in a bucket or directory:

```
<root>/acf.json                      repository descriptor (immutable after creation)
<root>/objects/<id[0:2]>/<id>        immutable, content-addressed objects
<root>/refs/branches/<name>          branch heads (mutable, compare-and-swap only)
<root>/refs/leases/<key>             leases on paths of shared branches (compare-and-swap only)
<root>/retractions/<id>              retraction records (immutable)
<root>/index/manifest                lineage index manifest (single writer: the compactor)
<root>/index/segments/<id>           lineage index segments (immutable)
```

Writers MUST NOT modify an object under `objects/`, `retractions/` or `index/segments/`
after it is written. Writers MUST create those with `If-None-Match: *`. A
`PreconditionFailed` response means the object already exists, and since objects are
content-addressed, it is the same object, so it counts as success.

### 2.1 Descriptor (`acf.json`)

```json
{"format": "agentcloud-repo", "version": 1, "created_at": 1763900000.0,
 "chunking": {"algorithm": "fastcdc-gear-v1", "min": 2048, "avg": 8192, "max": 65536},
 "tree": {"algorithm": "prolly-blake2b-v1", "leaf_target": 64, "internal_target": 64, "max_node": 1024},
 "encryption": {"scheme": "aes-256-gcm-envelope-v1", "kek": "local", "wrapped_dek": "<base64>", "id_key_wrapped": "<base64>"}}
```

`encryption` MAY be `{"scheme": "none"}`. All parameters are fixed for the life of a
repository: chunk and node boundaries must never change, or deduplication and structural
sharing break.

## 3. Objects

Every object is a typed JSON document, or for chunks raw bytes, identified by:

- **unencrypted:** `id = hex(SHA-256(type || 0x00 || plaintext))`
- **encrypted:** `id = hex(HMAC-SHA256(K_id, type || 0x00 || plaintext))`, with `K_id` a
  per-repository key. Identical content deduplicates within a repository but is not
  recognisable across repositories.

Stored bytes are `plaintext` (unencrypted), or `nonce(12) || AES-256-GCM(DEK, plaintext,
aad = id)` (encrypted). Readers MUST verify the id after decryption.

| Type | Plaintext | Purpose |
|---|---|---|
| `chunk` | raw bytes | A piece of file content, cut by FastCDC (§3.1) |
| `blob` | `{"size": n, "chunks": [[chunk_id, len], ...]}` | A file's content |
| `prov` | §5 | Provenance of one file version |
| `tree` | §4 | A prolly-tree node of a folder |
| `commit` | §6 | A snapshot of a branch |
| `edges` | `{"edges": [[input_prov, output_prov], ...]}` | Lineage edges introduced by a commit |

### 3.1 Chunking

`fastcdc-gear-v1`: the Gear rolling hash with the 256-entry table
`GEAR[i] = first 8 bytes (big-endian) of SHA-256("agentcloud-gear-" || decimal(i))`, and
normalised chunking with masks `0x0003590703530000` (before `avg`) and
`0x0000D90003530000` (after). There is no boundary check in the first `min` bytes, and a
forced cut at `max`. Files of at most `max` bytes are a single chunk.

## 4. Trees (prolly trees)

A folder is a prolly tree over its entries, sorted by `name` (bytewise UTF-8).

- **Entries:** `["f", name, blob_id, size, prov_id]` for files and `["d", name, tree_id]`
  for subfolders.
- **Leaf node:** `{"e": [entry, ...]}`.
- **Internal node:** `{"i": level, "c": [[first_name, child_id], ...]}`.
- **Cut rule (level L, target T):** a node ends after an item when it has at least `T/2`
  items and `uint64(BLAKE2b-64("L:" || key)) mod T == 0`, or when it reaches `max_node`
  items. The key is the entry name at level 0 and `first_name` above.
- A folder whose entries fit in one leaf is that leaf. The empty folder is the leaf
  `{"e":[]}`. Its id follows §3 (so it is keyed in encrypted repositories), and it need
  not be stored.
- Plaintexts are canonical JSON: keys sorted, no insignificant whitespace, UTF-8.

The cut rule depends only on the sorted items, so a given set of entries always produces
the same root id however it was built (history independence). Changing one entry
rewrites O(log n) nodes.

## 5. Provenance records

```json
{"writer": "agent:researcher-17", "task": "t-42", "model": "claude-opus-5-5",
 "path": "reports/q3.md", "blob": "<blob_id>", "inputs": ["<prov_id>", ...],
 "sources": ["https://example.com/page"], "trust": "trusted" | "untrusted",
 "ts": 1763900000.123, "nonce": "<16 hex chars>"}
```

- `inputs` are the provenance ids of the file versions the writer read.
- `trust` MUST be `untrusted` if `sources` is non-empty or any input is untrusted or
  retracted (§8).
- `nonce` keeps records unique when the same content is written twice.

## 6. Commits and the commit protocol

```json
{"tree": "<root tree id>", "parents": ["<commit_id>", ...], "branch": "agents/researcher-17",
 "author": "agent:researcher-17", "message": "...", "ts": 1763900000.5, "seq": 42,
 "edges": "<edges object id or null>", "fence": null}
```

`seq` is one more than the first parent's `seq` (0 for a branch's first commit).

### 6.1 Branch heads

`refs/branches/<name>` contains `{"commit": "<commit_id>", "seq": 42}`. It is changed only
by compare-and-swap.

### 6.2 Committing

1. Write every new `chunk`, `blob`, `prov`, `tree` and `edges` object (§2: create-only).
2. Read the branch head and keep its ETag (or note that it doesn't exist).
3. Write the `commit` object with `parents = [head.commit]`.
4. Write the head with `If-Match: <etag>`, or `If-None-Match: *` for a new branch.
5. On `PreconditionFailed`, another writer moved the branch. A writer on a single-writer
   branch SHOULD treat this as an error. On a shared branch, the writer re-reads the head,
   three-way merges its tree against the new head using the old head as the base (§7),
   and retries from step 3 with jittered exponential backoff.

Objects written by a commit that loses the race are unreferenced and are reclaimed by GC.

### 6.3 Leases (shared branches)

`refs/leases/<key>`, where `key = hex(SHA-256(branch || 0x00 || path))[0:32]`, contains
`{"holder": "...", "fence": n, "expires_at": t}`. To acquire a lease, a writer creates the
object, or replaces it with `If-Match` when it has expired, with `fence = previous + 1`. A
writer holding a lease puts its `fence` in the commit. When a branch head records a higher
fence for an overlapping path, readers MUST ignore commits carrying a lower one, and
writers SHOULD re-acquire the lease.

## 7. Merges

Merging `source` into `target`:

1. Find the merge base: the nearest common ancestor in the commit graph.
2. Three-way merge per path, comparing blob ids for base, ours (target) and theirs (source):
   - only one side changed: take that side;
   - both made the same change: take it;
   - both changed a text file: line-level merge (diff3 over histogram-diff alignment);
   - anything else is a conflict, resolved as `ours`, `theirs` or explicit content.
3. Write a commit with `parents = [target_head, source_head]` and compare-and-swap the
   target head as in §6.2.

**Hierarchical fan-in.** An integrator for a team branch merges many worker heads in one
commit (`parents = [team_head, w1, w2, ...]`), so contention on the team head is one CAS
per batch, not one per worker.

## 8. Lineage, retraction and taint

### 8.1 The lineage graph

Nodes are provenance ids, and edges run from `input → output`. Each commit stores the
edges it introduces in an `edges` object, so the graph is the union of all commits' edges.

### 8.2 The reverse index

Finding everything downstream of a node needs the edges by input. The compactor, a
single writer (one process, or elected by lease), maintains an LSM index on the object
store:

- **Segment:** an immutable object `{"edges": [[input, output], ...]}` sorted by `input`.
  Segments merge as they grow (sizes in powers of 4).
- **Manifest:** `index/manifest` = `{"segments": [...], "watermarks": {branch: commit_id}}`,
  updated by compare-and-swap.

A downstream query reads the segments plus the *tail*: edges from commits newer than each
branch's watermark, found by walking each branch from its head back to the watermark.
Results are exact. The tail keeps them current without the compactor being up to date.

### 8.3 Retraction

`retract(prov_id, reason)` writes `retractions/<id>`:

```json
{"root": "<prov_id>", "reason": "...", "by": "...", "ts": 1763900000.0,
 "affected": "<object id of a sorted list of downstream prov ids at retraction time>"}
```

A version is **retracted** if its provenance id is a retraction root, or is downstream of
one. Writers SHOULD check new inputs against the current retraction set (the union of
roots and `affected` lists, plus newer tail edges) and MUST mark the new record
`untrusted` if any input is retracted. A version derived after a retraction is therefore
flagged at write time.

### 8.4 Taint

Trust propagates through the same graph (§5). Retraction is a manual,
after-the-fact taint applied to everything downstream.

## 9. Garbage collection

An object is live if it is reachable from a branch head, a retraction record, or the index
manifest. GC MUST NOT delete objects younger than a grace period (default 24 hours), so a
commit in flight is never broken.

## 10. Consistency requirements

The object store MUST provide strong read-after-write consistency for single keys, and
atomic conditional `PUT` (`If-None-Match: *`, `If-Match`). Amazon S3, Google Cloud Storage
(via generation preconditions), Cloudflare R2, MinIO and local filesystems (with
`link`/`rename`) qualify.

## 11. Open questions (before 1.0)

- **Pack files.** In the reference implementation a one-file commit costs about 7.8 PUTs
  and 4.8 GETs: chunk, blob, provenance, tree nodes, commit and branch head. At S3 list
  prices that is about $41 per million commits. Bundling a commit's new objects into one
  immutable pack (with an index) would bring it to about 2 PUTs. This is the main cost
  lever at a million agents.
- **Index scale.** The reverse index is exact but keeps segments as sorted lists. At
  billions of edges it needs partitioned segments (by hash of `input`) with per-segment
  Bloom filters, so a query reads only the partitions it needs.
- **Lineage garbage collection.** Retraction records and edges outlive the objects GC
  removes. How long lineage is kept should be a repository policy.
- **Other object stores.** GCS uses generation preconditions (`ifGenerationMatch`) rather
  than ETags; the mapping should be specified per backend.
- **Fence enforcement.** Readers ignoring commits with stale fences (§6.3) is specified
  but not yet enforced by the reference implementation.

## 12. Conformance

The reference implementation is the Python package `agentcloud_core` in this repository.
Its test suite runs the same conformance tests against every backend (memory, local
filesystem, S3 via an emulator).
