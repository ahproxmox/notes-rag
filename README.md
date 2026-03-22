# notes-rag

Hybrid BM25 + vector RAG search engine for markdown workspaces. Indexes `.md` files, watches for changes in real time, and serves a FastAPI HTTP API for search, similarity, and web research.

## How it works

1. **Indexer** scans a workspace directory for `.md` files, splits them into chunks (markdown-aware, configurable size), and stores embeddings in ChromaDB
2. **Watcher** monitors the filesystem for create/modify/delete events and incrementally re-indexes affected files
3. **Search** combines BM25 keyword retrieval (40%) with semantic vector search (60%) via an ensemble retriever, then synthesises an answer using an LLM
4. **Direct lookup** shortcuts — queries like "todo 099" bypass the retriever entirely and return the file contents directly

## API

All endpoints are served by FastAPI on port `8080` (configurable via `RAG_PORT`).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/search` | Hybrid search with LLM synthesis. Body: `{"query": "...", "exclude_sources": []}` |
| `POST` | `/similar` | Vector similarity only, no LLM. Body: `{"query": "...", "k": 5}` |
| `POST` | `/research` | Web research pipeline — Brave Search → scrape → summarise → save. Body: `{"query": "..."}` |

## Setup

```bash
# Clone
git clone https://github.com/ahproxmox/notes-rag.git
cd notes-rag

# Create venv and install deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set OPENROUTER_API_KEY and BRAVE_API_KEY

cp indexer.yaml indexer.yaml.local
# Edit indexer.yaml — set workspace path and chroma_path

# Build initial index
python indexer.py

# Run (starts API + file watcher)
python main.py
```

## Configuration

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | API key for LLM synthesis (OpenRouter) |
| `BRAVE_API_KEY` | Yes | API key for web research (Brave Search) |
| `RAG_PORT` | No | API port (default: `8080`) |
| `RAG_CONFIG_PATH` | No | Path to indexer.yaml (default: `./indexer.yaml`) |
| `LLM_BASE_URL` | No | LLM API base URL (default: `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | No | LLM model ID (default: `google/gemini-2.5-flash-lite`) |
| `VAULT_ADDR` | No | HashiCorp Vault address — enables vault-fetch.py on startup |

### indexer.yaml

```yaml
workspace: /mnt/Claude        # directory to index
exclude: [trash, tmp, temp]   # subdirectories to skip
chunk_size: 300                # characters per chunk
chunk_overlap: 60              # overlap between chunks
embedding_model: all-MiniLM-L6-v2
chroma_path: ./chroma          # where ChromaDB stores data
watch_extra:                   # optional extra directories to watch
  - /mnt/Obsidian
```

## Production deployment

A systemd service file is provided at `deploy/rag.service`. Copy it to `/etc/systemd/system/` and adjust paths:

```bash
cp deploy/rag.service /etc/systemd/system/rag.service
systemctl daemon-reload
systemctl enable --now rag
```

## Vault integration

If `VAULT_ADDR` is set, `main.py` runs `vault-fetch.py` on startup to pull secrets from HashiCorp Vault via AppRole auth and write them to `.env`. This requires `/etc/vault/role-id` and `/etc/vault/secret-id` files. If `VAULT_ADDR` is not set, vault-fetch is skipped and secrets are read from `.env` directly.

## Evaluation

The `bench/` directory contains a scoring suite for measuring retrieval quality:

```bash
# Run queries against a RAG endpoint
python bench/run_bench.py --endpoint http://localhost:8080 --output bench/results.json

# Score the results
python bench/score.py bench/results.json
```

Queries are defined in `bench/queries.yaml`. Scoring methods:
- **contains**: checks if expected terms appear in the answer
- **refuse**: checks if the system correctly refuses to reveal sensitive information

## Architecture

```
main.py          → entry point: vault-fetch → load .env → start watcher thread → start API
api.py           → FastAPI app, lazy chain building with thread-safe rebuild on dirty flag
search.py        → hybrid retrieval (BM25 + vector ensemble), LLM synthesis, todo lookup shortcut
indexer.py       → markdown-aware chunking, ChromaDB storage, full and incremental indexing
watcher.py       → watchdog filesystem observer, triggers re-index + chain invalidation
research.py      → Brave Search → scrape → LLM summarise → save to workspace inbox
vault-fetch.py   → optional Vault AppRole auth → write secrets to .env
```

## Stack

- Python 3.11+
- FastAPI + Uvicorn
- LangChain (retrieval, chains, embeddings)
- ChromaDB (vector store)
- sentence-transformers / all-MiniLM-L6-v2 (embeddings)
- rank-bm25 (keyword search)
- PyTorch CPU
