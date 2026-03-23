from fastapi import FastAPI
from pydantic import BaseModel
from search import search, search_filtered, search_with_weights, similar
from research import research

app = FastAPI()


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
        answer, sources = search_filtered(req.query, req.exclude_sources)
    else:
        answer, sources = search(req.query)
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
