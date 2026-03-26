import os
import re
import glob as globmod
import yaml
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


def _retrieve(query: str, k: int = 20, bm25_weight: float = 0.4, vector_weight: float = 0.6,
              folder: str | None = None) -> list[Document]:
    """Hybrid retrieval: FTS5 keyword + sqlite-vec vector, merged via RRF.

    Retrieves k candidates from each source, then merges with weighted
    reciprocal rank fusion (RRF) scoring. Returns top-k unique documents.
    """
    store = get_store()

    bm25_docs = store.search_bm25(query, k=k, folder=folder)
    vector_docs = store.search_vector(query, k=k, folder=folder)

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
        doc_map[key] = doc

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


def search(query: str, bm25_weight: float = 0.4, vector_weight: float = 0.6,
           final_k: int = 6, folder: str | None = None) -> tuple[str, list[str], list[dict]]:
    """Full RAG search: retrieve top-20, rerank to top-6, synthesise."""
    lookup = _try_todo_lookup(query)
    if lookup:
        answer, sources = lookup
        return answer, sources, []

    docs = _retrieve(query, k=20, bm25_weight=bm25_weight, vector_weight=vector_weight, folder=folder)
    if not docs:
        return "No relevant context found in the workspace.", [], []

    reranker = _get_reranker()
    if reranker:
        docs = reranker.rerank(query, docs, top_k=final_k)
    else:
        docs = docs[:final_k]

    answer = _synthesise(query, docs)
    sources = list(dict.fromkeys(d.metadata.get('filename', 'unknown') for d in docs))
    return answer, sources, _docs_to_chunks(docs)


def search_with_weights(query: str, bm25_weight: float, vector_weight: float) -> tuple[str, list[str], list[dict]]:
    """Run a search with custom ensemble weights."""
    return search(query, bm25_weight=bm25_weight, vector_weight=vector_weight)


def search_filtered(query: str, exclude_sources: list[str], folder: str | None = None) -> tuple[str, list[str], list[dict]]:
    """Retrieve docs, filter out excluded source files, rerank, then synthesise."""
    lookup = _try_todo_lookup(query)
    if lookup:
        answer, sources = lookup
        return answer, sources, []

    docs = _retrieve(query, k=20, folder=folder)
    docs = [d for d in docs if d.metadata.get('filename', '') not in exclude_sources]
    if not docs:
        return "No relevant context found in the workspace.", [], []

    reranker = _get_reranker()
    if reranker:
        docs = reranker.rerank(query, docs, top_k=6)
    else:
        docs = docs[:6]

    answer = _synthesise(query, docs)
    sources = list(dict.fromkeys(d.metadata.get('filename', 'unknown') for d in docs))
    return answer, sources, _docs_to_chunks(docs)


def similar(query: str, k: int = 5) -> list[dict]:
    """Return top-k similar documents by vector similarity. No LLM call."""
    store = get_store()
    docs = store.search_vector(query, k=k)
    return [
        {
            'content': doc.page_content,
            'source': doc.metadata.get('filename', 'unknown'),
            'score': 0.0,
        }
        for doc in docs
    ]


if __name__ == '__main__':
    import sys
    query = ' '.join(sys.argv[1:]) or 'What SSH setup do I have?'
    print(f'Query: {query}\n')
    answer, sources = search(query)
    print(f'Answer:\n{answer}\n')
    print(f'Sources: {sources}')
