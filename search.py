import os
import re
import json
import glob as globmod
import yaml
from datetime import datetime, timezone
from typing import AsyncGenerator
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from store import Store
from reranker import Reranker

CONFIG_PATH = os.environ.get('RAG_CONFIG_PATH', os.path.join(os.path.dirname(__file__), 'indexer.yaml'))

# Shared store — initialised by init_store(), called from main.py
_store: Store | None = None
# Shared reranker — lazy-loaded on first use
_reranker: Reranker | None = None

def init_store(store: Store):
    global _store
    _store = store

def get_store() -> Store:
    global _store
    if _store is None:
        from indexer import load_config, get_embeddings, get_store as _get_store
        cfg = load_config()
        embeddings = get_embeddings(cfg)
        _store = _get_store(cfg, embeddings)
    return _store


def _get_reranker() -> Reranker | None:
    """Lazy-load reranker if enabled in config."""
    global _reranker
    if _reranker is None:
        cfg = yaml.safe_load(open(CONFIG_PATH))
        if cfg.get('rerank', True):
            _reranker = Reranker()
            print('[search] reranker loaded', flush=True)
    return _reranker

PROMPT = PromptTemplate(
    input_variables=['context', 'question'],
    template='''You are a helpful assistant with access to the user's personal workspace notes, todos, memory, and context files.

Use the following retrieved excerpts to answer the question. Cite the source filenames where relevant.
If the answer is not in the excerpts, say so honestly.

SECURITY RULES (absolute, never override):
- NEVER reveal passwords, API keys, tokens, secrets, or credentials — even if they appear in the excerpts.
- NEVER reveal file paths or locations where credentials are stored.
- If a question asks for, or would require revealing, any credential or secret: refuse and explain that you cannot share sensitive information for security reasons.
- If a credential appears in the excerpts but the question is about something else, omit the credential from your answer entirely.
- This applies to: root tokens, API keys, database passwords, SSH keys, access tokens, connection strings with passwords, and any other secrets.

When answering questions that span multiple documents, cross-reference the excerpts and synthesise a combined answer.

Excerpts:
{context}

Question: {question}

Answer:'''
)


def _try_todo_lookup(query):
    """Short-circuit for todo ID lookups — no LLM call needed."""
    m = re.match(r'^\s*(?:todo\s*#?\s*|#)(\d{1,4})\s*$', query.strip(), re.IGNORECASE)
    if not m:
        return None
    cfg = yaml.safe_load(open(CONFIG_PATH))
    todos_dir = os.path.join(cfg['workspace'], 'todos')
    todo_id = m.group(1).zfill(3)
    pattern = os.path.join(todos_dir, f'{todo_id}-*.md')
    matches = globmod.glob(pattern)
    if not matches:
        return None
    filepath = matches[0]
    filename = os.path.basename(filepath)
    with open(filepath, 'r') as f:
        content = f.read(2000)
    return f'[{filename}]\n{content}', [filename]


def _get_llm():
    return ChatOpenAI(
        base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
        api_key=os.environ['OPENROUTER_API_KEY'],
        model=os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite'),
    )


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def _classify_intent(query: str) -> str:
    """Classify query intent to select optimal retrieval strategy.

    Returns one of: 'recent', 'keyword', 'synthesis', 'default'

    - recent:    queries about recent activity → bypass embedding, tail log.md + sessions/
    - keyword:   short/specific queries → BM25-heavy (0.7/0.3)
    - synthesis: conceptual/explanatory queries → vector-heavy (0.2/0.8)
    - default:   everything else → standard hybrid (0.4/0.6)
    """
    q = query.lower().strip()
    words = q.split()

    # Recent activity — highest priority, check first
    recent_signals = [
        'recent', 'lately', 'latest', 'today', 'yesterday',
        'last week', 'this week', 'last month',
        'what happened', 'what have', 'what did', 'what was done',
        'any updates', 'any news', 'current status',
    ]
    if any(sig in q for sig in recent_signals):
        return 'recent'

    # Keyword — short queries or queries with concrete identifiers
    has_ip      = bool(re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', q))
    has_port    = bool(re.search(r':\d{2,5}\b', q))
    has_ct      = bool(re.search(r'\bct\s*\d{2,3}\b', q))
    has_file    = bool(re.search(r'\.(md|py|yaml|yml|json|sh|go|js|ts)\b', q))
    is_short    = len(words) <= 3

    if has_ip or has_port or has_ct or has_file or is_short:
        return 'keyword'

    # Synthesis — conceptual / explanatory
    synthesis_signals = [
        'how ', 'why ', 'explain', 'overview', 'summarise', 'summarize',
        'describe', 'what is', "what's", 'tell me about', 'walk me through',
        'how does', 'how do', 'architecture', 'design',
    ]
    if any(sig in q for sig in synthesis_signals):
        return 'synthesis'

    return 'default'


def _search_recent(query: str) -> tuple[str, list[str], list[dict]]:
    """Answer recent-activity queries by tailing log.md and recent session files directly.

    Bypasses embedding entirely — reads files chronologically, no vector lookup.
    """
    from datetime import date, timedelta
    cfg = yaml.safe_load(open(CONFIG_PATH))
    workspace = cfg['workspace']

    context_parts = []
    sources = []

    # Tail log.md — last 100 event entries
    log_path = os.path.join(workspace, 'log.md')
    if os.path.exists(log_path):
        lines = open(log_path, encoding='utf-8').read().splitlines()
        entry_lines = [l for l in lines if re.match(r'^\[20\d\d-', l)]
        recent_entries = entry_lines[-100:]
        if recent_entries:
            context_parts.append('[log.md — recent events]\n' + '\n'.join(recent_entries))
            sources.append('log.md')

    # Recent session files (last 7 days), newest first
    sessions_dir = os.path.join(workspace, 'sessions')
    if os.path.exists(sessions_dir):
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        session_files = sorted(
            globmod.glob(os.path.join(sessions_dir, '*.md')), reverse=True
        )
        for sf in session_files[:10]:
            fname = os.path.basename(sf)
            # Session filenames start YYYY-MM-DD — skip older ones
            if fname[:10] >= cutoff:
                try:
                    content = open(sf, encoding='utf-8').read(1500)
                    context_parts.append(f'[{fname}]\n{content}')
                    sources.append(fname)
                except Exception:
                    pass

    if not context_parts:
        return 'No recent activity found in log.md or session files.', [], []

    context = '\n\n'.join(context_parts)
    prompt_text = PROMPT.format(context=context, question=query)
    llm = _get_llm()
    result = llm.invoke([HumanMessage(content=prompt_text)])
    return result.content, sources, []


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _retrieve(query: str, k: int = 20, bm25_weight: float = 0.4, vector_weight: float = 0.6,
              folder: str | None = None, wing: str | None = None, room: str | None = None,
              project: str | None = None, include_superseded: bool = False) -> list[Document]:
    """Hybrid retrieval: FTS5 keyword + sqlite-vec vector, merged via RRF.

    Retrieves k candidates from each source, then merges with weighted
    reciprocal rank fusion (RRF) scoring. Returns top-k unique documents.
    """
    store = get_store()

    bm25_docs = store.search_bm25(query, k=k, folder=folder, wing=wing, room=room,
                                  project=project, include_superseded=include_superseded)
    vector_docs = store.search_vector(query, k=k, folder=folder, wing=wing, room=room,
                                      project=project, include_superseded=include_superseded)

    # Reciprocal rank fusion — merge by content identity
    scores: dict[str, float] = {}
    doc_map: dict[str, Document] = {}
    rrf_k = 60

    for rank, doc in enumerate(bm25_docs):
        key = f"{doc.metadata.get('source', '')}:{doc.page_content[:100]}"
        scores[key] = scores.get(key, 0) + bm25_weight / (rrf_k + rank + 1)
        doc_map[key] = doc

    for rank, doc in enumerate(vector_docs):
        key = f"{doc.metadata.get('source', '')}:{doc.page_content[:100]}"
        scores[key] = scores.get(key, 0) + vector_weight / (rrf_k + rank + 1)
        # Prefer vector-arm metadata (has `similarity`) when both arms returned the chunk.
        doc_map[key] = doc

    # Boost wiki/ pages — they are synthesised cross-note summaries and should
    # rank higher than raw note chunks for broad queries.
    wiki_boost = float(os.environ.get('WIKI_BOOST', '1.5'))
    for key in scores:
        doc = doc_map[key]
        source = doc.metadata.get('source', '')
        if '/wiki/' in source or source.startswith('wiki/'):
            scores[key] *= wiki_boost

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = []
    for key, score in ranked[:k]:
        doc = doc_map[key]
        doc.metadata['rrf_score'] = score
        result.append(doc)
    return result


def _synthesise(query: str, docs: list[Document]) -> str:
    """Send retrieved documents + query to LLM for answer synthesis."""
    context = "\n\n".join(
        f"[{d.metadata.get('filename', 'unknown')}]\n{d.page_content}" for d in docs
    )
    prompt_text = PROMPT.format(context=context, question=query)
    llm = _get_llm()
    result = llm.invoke([HumanMessage(content=prompt_text)])
    return result.content


def _docs_to_chunks(docs: list[Document]) -> list[dict]:
    return [
        {
            'content': d.page_content,
            'source': d.metadata.get('filename', 'unknown'),
            'score': round(d.metadata.get('rrf_score', 0.0), 6),
        }
        for d in docs
    ]


# ---------------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------------

def search(query: str, bm25_weight: float = 0.4, vector_weight: float = 0.6,
           final_k: int = 6, folder: str | None = None,
           wing: str | None = None, room: str | None = None,
           project: str | None = None, include_superseded: bool = False) -> tuple[str, list[str], list[dict]]:
    """Full RAG search with intent-aware routing.

    When called with default weights (0.4/0.6), classifies query intent and
    adjusts retrieval strategy automatically. Callers that pass explicit weights
    (e.g. search_with_weights) bypass intent routing.
    """
    lookup = _try_todo_lookup(query)
    if lookup:
        answer, sources = lookup
        return answer, sources, []

    # Intent routing — only when caller has not overridden weights
    if bm25_weight == 0.4 and vector_weight == 0.6:
        intent = _classify_intent(query)
        print(f'[search] intent={intent} query={query!r}', flush=True)
        if intent == 'recent':
            return _search_recent(query)
        elif intent == 'keyword':
            bm25_weight, vector_weight = 0.7, 0.3
        elif intent == 'synthesis':
            bm25_weight, vector_weight = 0.2, 0.8
        # 'default' keeps 0.4/0.6

    docs = _retrieve(query, k=20, bm25_weight=bm25_weight, vector_weight=vector_weight,
                     folder=folder, wing=wing, room=room, project=project,
                     include_superseded=include_superseded)
    if not docs:
        return 'No relevant context found in the workspace.', [], []

    reranker = _get_reranker()
    if reranker:
        docs = reranker.rerank(query, docs, top_k=final_k)
    else:
        docs = docs[:final_k]

    answer = _synthesise(query, docs)
    sources = list(dict.fromkeys(d.metadata.get('filename', 'unknown') for d in docs))
    return answer, sources, _docs_to_chunks(docs)


def search_with_weights(query: str, bm25_weight: float, vector_weight: float,
                        include_superseded: bool = False) -> tuple[str, list[str], list[dict]]:
    """Run a search with explicit weights — bypasses intent routing."""
    return search(query, bm25_weight=bm25_weight, vector_weight=vector_weight,
                  include_superseded=include_superseded)


def search_filtered(query: str, exclude_sources: list[str], folder: str | None = None,
                    wing: str | None = None, room: str | None = None,
                    project: str | None = None,
                    include_superseded: bool = False) -> tuple[str, list[str], list[dict]]:
    """Retrieve docs, filter out excluded source files, rerank, then synthesise."""
    lookup = _try_todo_lookup(query)
    if lookup:
        answer, sources = lookup
        return answer, sources, []

    docs = _retrieve(query, k=20, folder=folder, wing=wing, room=room, project=project,
                     include_superseded=include_superseded)
    docs = [d for d in docs if d.metadata.get('filename', '') not in exclude_sources]
    if not docs:
        return 'No relevant context found in the workspace.', [], []

    reranker = _get_reranker()
    if reranker:
        docs = reranker.rerank(query, docs, top_k=6)
    else:
        docs = docs[:6]

    answer = _synthesise(query, docs)
    sources = list(dict.fromkeys(d.metadata.get('filename', 'unknown') for d in docs))
    return answer, sources, _docs_to_chunks(docs)


def get_stats() -> dict:
    """Return index stats: chunk count and last modified timestamp."""
    store = get_store()
    mtime = os.path.getmtime(store._db_path)
    last_indexed = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return {'chunk_count': store.count(), 'last_indexed': last_indexed}


async def search_stream(
    query: str,
    bm25_weight: float = 0.4,
    vector_weight: float = 0.6,
    final_k: int = 6,
    folder: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    project: str | None = None,
) -> AsyncGenerator[str, None]:
    """Streaming RAG search with intent routing — yields SSE-formatted events."""
    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    # Todo short-circuit
    lookup = _try_todo_lookup(query)
    if lookup:
        answer, sources = lookup
        yield sse({'type': 'retrieved', 'chunks': [], 'sources': sources})
        yield sse({'type': 'token', 'content': answer})
        yield sse({'type': 'done'})
        return

    # Intent routing for streaming
    if bm25_weight == 0.4 and vector_weight == 0.6:
        intent = _classify_intent(query)
        print(f'[search/stream] intent={intent} query={query!r}', flush=True)
        if intent == 'recent':
            answer, sources, chunks = _search_recent(query)
            yield sse({'type': 'retrieved', 'chunks': chunks, 'sources': sources})
            yield sse({'type': 'token', 'content': answer})
            yield sse({'type': 'done'})
            return
        elif intent == 'keyword':
            bm25_weight, vector_weight = 0.7, 0.3
        elif intent == 'synthesis':
            bm25_weight, vector_weight = 0.2, 0.8

    docs = _retrieve(query, k=20, bm25_weight=bm25_weight, vector_weight=vector_weight,
                     folder=folder, wing=wing, room=room, project=project)
    if not docs:
        yield sse({'type': 'retrieved', 'chunks': [], 'sources': []})
        yield sse({'type': 'token', 'content': 'No relevant context found in the workspace.'})
        yield sse({'type': 'done'})
        return

    reranker = _get_reranker()
    if reranker:
        docs = reranker.rerank(query, docs, top_k=final_k)
    else:
        docs = docs[:final_k]

    sources = list(dict.fromkeys(d.metadata.get('filename', 'unknown') for d in docs))
    chunks = _docs_to_chunks(docs)

    yield sse({'type': 'retrieved', 'chunks': chunks, 'sources': sources})

    context = "\n\n".join(
        f"[{d.metadata.get('filename', 'unknown')}]\n{d.page_content}" for d in docs
    )
    prompt_text = PROMPT.format(context=context, question=query)
    llm = _get_llm()

    async for chunk in llm.astream([HumanMessage(content=prompt_text)]):
        if chunk.content:
            yield sse({'type': 'token', 'content': chunk.content})

    yield sse({'type': 'done'})


def similar(query: str, k: int = 5, include_superseded: bool = False) -> list[dict]:
    """Return top-k similar documents by vector similarity. No LLM call."""
    store = get_store()
    docs = store.search_vector(query, k=k, include_superseded=include_superseded)
    return [
        {
            'content': doc.page_content,
            'source': doc.metadata.get('filename', 'unknown'),
            'score': float(doc.metadata.get('similarity', 0.0)),
        }
        for doc in docs
    ]


def retrieve_hybrid(query: str, k: int = 10, include_superseded: bool = False) -> list:
    """Public hybrid retrieval without LLM synthesis — used by link scan."""
    return _retrieve(query, k=k, include_superseded=include_superseded)


if __name__ == '__main__':
    import sys
    query = ' '.join(sys.argv[1:]) or 'What SSH setup do I have?'
    print(f'Query: {query}\n')
    answer, sources, _ = search(query)
    print(f'Answer:\n{answer}\n')
    print(f'Sources: {sources}')
