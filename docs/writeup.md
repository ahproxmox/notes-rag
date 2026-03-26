# Building a Self-Hosted RAG Engine: Decisions, Experiments, and What Broke

This is a writeup of the engineering decisions behind notes-rag — a hybrid retrieval-augmented generation system I built to make my personal markdown workspace queryable. It covers why I built it, the architecture choices, what the evaluation revealed, and the production bugs I ran into.

---

## Why I built this

I keep notes, todos, infrastructure context, and decision logs across hundreds of markdown files. Standard search (grep, Obsidian search) is fine for exact lookups but falls apart when you want to ask things like "what did I decide about the Redis caching approach?" or "what SSH config do I have set up?" — questions where the answer is spread across multiple files or uses different wording than you remember.

The goal was a system that could answer those questions accurately, run entirely on my homelab, and stay current as files change.

---

## Why hybrid retrieval, not just vector search

Pure vector search (semantic similarity) is good at handling paraphrasing and conceptual queries but misses exact terms — IPs, hostnames, version numbers, specific file names. Pure BM25 (keyword search) nails exact terms but fails on semantic intent.

For a workspace full of technical notes, both failure modes matter. Hybrid retrieval using Reciprocal Rank Fusion (RRF) combines both result lists by rank position rather than score, which avoids the score normalisation problem that plagues naive score-weighted fusion.

The weight sweep (7 BM25/vector combinations from 0.0/1.0 to 1.0/0.0) showed that answer quality was nearly flat across all combinations — a useful negative result. It tells you that once you have a good chunking strategy, ensemble weighting is a second-order concern. Production uses 0.4/0.6 based on common guidance, but any reasonable split works.

---

## The chunk size experiment

Chunk size has a tension at its core: larger chunks have better recall (the relevant information is more likely to be inside the chunk) but worse precision (the LLM gets more noise per chunk). I tested four sizes — 300, 500, 800, 1200 — with 20% overlap each.

Results from the 43-query eval set:

| Chunk size | Answer score | MRR |
|------------|-------------|-----|
| 300 | 73% | 0.61 |
| 500 | 68% | 0.64 |
| 800 | 65% | 0.67 |
| 1200 | 61% | 0.69 |

MRR (retrieval recall) improves with larger chunks, as expected — the target content is more likely to be inside a bigger chunk. But answer quality degrades. At 1200 chars, the LLM is synthesising from passages where the relevant sentence is buried among a lot of unrelated context, and answer quality suffers.

chunk_size=300 is the sweet spot for this workload: short, precise excerpts that give the LLM exactly what it needs without the surrounding noise.

---

## Why SQLite over a dedicated vector store

The initial implementation used ChromaDB. I replaced it with sqlite-vec for a few reasons:

**Single dependency.** sqlite-vec runs inside SQLite — the same database that holds the FTS5 index. No separate process, no network socket, no separate data directory to manage. Everything is one `rag.db` file.

**Simpler operations.** Backup, restore, and inspection are just file operations. You can open the DB with any SQLite client and query the chunks table directly.

**Good enough performance.** At ~120k chunks the query latency is well within acceptable range (<500ms for retrieval). ChromaDB's advantage is at much larger scale.

The FTS5 keyword index runs in the same DB. `upsert_file` atomically replaces both the FTS5 entries and the vector embeddings for a source file, which keeps the two indexes in sync.

---

## The reranker

Embeddings are trained to measure semantic similarity at the sentence/paragraph level, which makes them good for retrieval ranking but not precise enough for top-k selection. A cross-encoder reranker takes each (query, chunk) pair and scores it directly — more expensive per call, but much more accurate.

The pipeline retrieves 20 candidates from RRF, then the reranker trims to 6. This two-stage approach keeps retrieval fast (approximate ANN search on 20 candidates) while improving final quality.

---

## The todo lookup shortcut

Queries like "todo 099" or "#087" short-circuit the retrieval pipeline entirely: the system regex-matches the ID, glob-searches the todos directory, and returns the raw file contents. No embedding, no LLM call, sub-100ms response.

This was worth building because "what's in todo N?" is a common query pattern where exact retrieval is trivially possible and semantic search adds noise.

---

## Production bugs

### The stale BM25 index

The `EnsembleRetriever` chain was cached globally at startup. The BM25 index was a snapshot of the documents at init time. New files were getting indexed into the vector store and FTS5 but the BM25 index in the cached chain never updated — so roughly 40% of the ensemble was always querying a stale snapshot.

Fix: added a `_chain_dirty` flag and `invalidate_chain()` function. The watcher calls `invalidate_chain()` after each file event. The API rebuilds the chain under a lock when the dirty flag is set.

### OOM kills during bulk indexing

The watchdog watcher received filesystem events for every file a batch operation touched. Processing them concurrently caused memory spikes that triggered the kernel OOM killer.

Fix: serialised the indexing queue — events are processed one at a time via a thread-safe queue. The service has `Restart=on-failure` so OOM kills trigger auto-recovery, but the serialisation prevents the spike in the first place.

A second OOM issue appeared when a doc scraper dumped 1000+ scraped external files into the watched workspace directory. Those files were irrelevant to workspace search — they were reference docs, not notes. Fix: added the `docs-archive` directory to the `exclude` list in `indexer.yaml`.

### Credential leaks

Three separate credentials appeared in LLM responses during eval: a Vault root token, an OpenRouter API key, and a CouchDB password. The LLM was faithfully summarising the retrieved chunks, which happened to contain those values in context files.

Fix: added explicit security rules to the system prompt banning credential disclosure regardless of retrieved content. Tested with 5 targeted queries (asking for the password, asking what the Redis password is, asking for API keys). All now correctly refuse.

The broader lesson: if you index files that contain secrets, the RAG will find them. Either exclude those files from the index or treat credential refusal as a first-class requirement with its own eval queries.

---

## Streaming

The initial implementation returned the full answer after synthesis completed — 3–5 seconds of waiting. Adding streaming required two things:

1. An async generator (`search_stream`) that yields SSE events: a `retrieved` event immediately after retrieval/rerank (before the LLM starts), then `token` events as the LLM streams, then `done`.

2. The `retrieved` event fires with the full chunk data before the LLM begins. The frontend uses this to populate the sources sidebar while the answer is still generating — so the latency feels much shorter even when total time is the same.

---

## What I'd do differently

**Smarter chunking.** Fixed-size character chunking ignores document structure. A markdown-aware chunker that splits on headings and preserves hierarchy would produce more semantically coherent chunks, particularly for structured notes.

**Async indexing with backpressure.** The current queue is synchronous. Large batch operations block the watcher thread. An async indexer with rate limiting and priority queuing would handle bulk loads more gracefully.

**Metadata filtering before retrieval.** The FTS5 search supports folder filtering, but the vector search pre-filter is approximate (sqlite-vec `k` is a pre-filter, not post-filter). Adding proper metadata-aware retrieval would let you scope queries to specific subdirectories reliably.

**Eval automation.** The benchmark runs manually. Hooking it into CI to track regression over time would catch quality degradation from prompt or config changes before they reach production.
