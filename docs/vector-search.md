# Vector search

Keyword search finds the words you typed. Vector search finds passages that mean the same
thing ("revenue forecast" finds "projected sales next quarter") and needs an embedding model.
Agent Cloud ships the whole pipeline. The model is your choice, and nothing downloads unless
you allow it.

## Turn it on

| `AGENTCLOUD_EMBEDDER` | What it is |
|---|---|
| `none` (default) | Off. `cloud_search` is keyword-only. |
| `hashing` | Built in, no download. Feature hashing of words and character trigrams. **Lexical, not semantic:** it matches shared words and spellings, not meaning. For tests and as a fallback. |
| `sentence-transformers:<model>` | A local model, e.g. `sentence-transformers:all-MiniLM-L6-v2`. Requires `pip install "agentcloud[local-embeddings]"`. Loads **only from the local cache** unless `AGENTCLOUD_EMBEDDER_ALLOW_DOWNLOAD=1`. |
| `http:<url>\|<model>` | Any OpenAI-compatible embeddings endpoint: OpenAI, Ollama (`http:http://localhost:11434/v1/embeddings\|nomic-embed-text`), vLLM, TEI. Key from `AGENTCLOUD_EMBEDDER_API_KEY`. **File contents are sent to that endpoint.** |

Check it with `GET /v1/vector/status`: the embedder, indexed chunks, and files waiting to be embedded.

## Use it

```text
cloud_search(query="projected sales next quarter", mode="semantic")
cloud_search(query="zebrafish regeneration", mode="hybrid")
cloud_search(query="...")          # mode=auto: hybrid when an embedder is configured, else keyword
```

REST: `GET /v1/search?q=...&mode=hybrid`.

| Mode | Ranking |
|---|---|
| `keyword` | BM25 with proximity, exact top-k (see [trust docs](trust-and-provenance.md) for the encrypted index) |
| `semantic` | Cosine similarity of chunk embeddings; a file scores its best chunk |
| `hybrid` | Both lists fused with **Reciprocal Rank Fusion** (score = Σ 1/(60 + rank)). `matched_in` says which lists found each file. |

## How it works

- **Chunks:** the same 40-line chunks as keyword search, embedded with their file path.
  Results point at the matching lines.
- **Indexing:** every write or move queues the file (a database trigger). Each search embeds
  up to 64 queued files first, so fast embedders give read-your-writes. The worker embeds
  the rest in the background, so slow models never block writes. Results report
  `embedding_pending` while that's happening.
- **Encrypted at rest:** vectors are AES-GCM encrypted with a key derived from the master
  key, because embeddings can be inverted to recover the text.
- **HNSW index:** an approximate nearest-neighbour graph (Malkov & Yashunin) per workspace
  per process, with the diversity heuristic for neighbour selection.
  - It is kept current by replaying a change log, so every server process sees new vectors.
  - Permission and path filters run inside the search. When a filter is too selective, it
    falls back to an exact scan, so results are never silently missing.
- **Model changes:** vectors are tagged with the model name. Switch models and each workspace
  re-embeds on its next search.

## Measured (MacBook, numpy installed)

| | |
|---|---|
| HNSW query, 20,000 × 384-dim vectors | **0.65 ms p50, recall@10 = 1.000** vs exact (brute force with numpy: 2.1 ms) |
| Hashing embedder | ~13,700 chunks/s |
| HNSW build | 43 s for 20,000 vectors (pure Python graph construction) |

## Limits

- **The graph is built in memory on a workspace's first semantic search after a restart**
  (about 2 ms per vector), and isn't persisted yet. For large workspaces, persisting the graph
  or a native HNSW (hnswlib) is the next step.
- **Without numpy,** HNSW runs in pure Python. That's fine for thousands of chunks, slow beyond.
- **The `hashing` embedder is not semantic.** Use a real model for meaning-based search.
