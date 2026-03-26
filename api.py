from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from search import search, search_filtered, search_with_weights, similar
from research import research
import os

app = FastAPI()

_ui_dir = os.path.join(os.path.dirname(__file__), 'ui')
app.mount('/ui', StaticFiles(directory=_ui_dir), name='ui')


class SearchRequest(BaseModel):
    query: str
    exclude_sources: list[str] = []
    bm25_weight: float | None = None
    vector_weight: float | None = None
    folder: str | None = None


class SimilarRequest(BaseModel):
    query: str
    k: int = 5


class ResearchRequest(BaseModel):
    query: str


@app.get('/')
def index():
    return FileResponse(os.path.join(_ui_dir, 'index.html'))


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/search')
def search_endpoint(req: SearchRequest):
    if req.bm25_weight is not None and req.vector_weight is not None:
        answer, sources, chunks = search_with_weights(req.query, req.bm25_weight, req.vector_weight)
    elif req.exclude_sources:
        answer, sources, chunks = search_filtered(req.query, req.exclude_sources, folder=req.folder)
    else:
        answer, sources, chunks = search(req.query, folder=req.folder)
    return {'answer': answer, 'sources': sources, 'chunks': chunks}


@app.post('/similar')
def similar_endpoint(req: SimilarRequest):
    """Return top-k similar documents by vector similarity. No LLM call."""
    results = similar(req.query, k=req.k)
    return {'results': results}


@app.post('/research')
def research_endpoint(req: ResearchRequest):
    summary, filepath = research(req.query)
    return {'summary': summary, 'filepath': filepath}
