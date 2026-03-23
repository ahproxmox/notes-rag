import threading
from fastapi import FastAPI
from pydantic import BaseModel
from search import get_chain, search, search_filtered, search_with_weights, similar, _chain_dirty
from research import research

app = FastAPI()
_chain = None
_chain_lock = threading.Lock()

def chain():
    global _chain
    with _chain_lock:
        if _chain is None or _chain_dirty.is_set():
            print('[api] (re)building chain...', flush=True)
            _chain = get_chain()
            _chain_dirty.clear()
    return _chain

class SearchRequest(BaseModel):
    query: str
    exclude_sources: list[str] = []
    bm25_weight: float | None = None
    vector_weight: float | None = None

class SimilarRequest(BaseModel):
    query: str
    k: int = 5

class ResearchRequest(BaseModel):
    query: str

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.post('/search')
def search_endpoint(req: SearchRequest):
    if req.bm25_weight is not None and req.vector_weight is not None:
        answer, sources = search_with_weights(req.query, req.bm25_weight, req.vector_weight)
    elif req.exclude_sources:
        c = chain()
        answer, sources = search_filtered(req.query, req.exclude_sources, c)
    else:
        c = chain()
        answer, sources = search(req.query, c)
    return {'answer': answer, 'sources': sources}

@app.post('/similar')
def similar_endpoint(req: SimilarRequest):
    """Return top-k similar documents by vector similarity. No LLM call."""
    results = similar(req.query, k=req.k)
    return {'results': results}

@app.post('/research')
def research_endpoint(req: ResearchRequest):
    summary, filepath = research(req.query)
    return {'summary': summary, 'filepath': filepath}
