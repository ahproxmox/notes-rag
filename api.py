from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from search import search, search_filtered, search_with_weights, search_stream, similar, get_stats
from research import research
import os
from pathlib import Path

app = FastAPI()

_ui_dir = os.path.join(os.path.dirname(__file__), 'ui')
app.mount('/ui', StaticFiles(directory=_ui_dir), name='ui')

_log_path = Path('/mnt/Claude/log.md')


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


@app.get('/stats')
def stats():
    return get_stats()


@app.get('/log/recent')
def log_recent(n: int = Query(default=50, ge=1, le=500)):
    """Return the last N lines of the workspace event log. No embedding — direct tail."""
    if not _log_path.exists():
        return {'lines': [], 'path': str(_log_path), 'error': 'log.md not found'}
    text = _log_path.read_text(encoding='utf-8', errors='replace')
    # Skip header lines (before the --- separator), return only log entries
    all_lines = text.splitlines()
    entry_lines = [l for l in all_lines if l.startswith('[20')]
    recent = entry_lines[-n:]
    return {'lines': recent, 'total': len(entry_lines), 'returned': len(recent)}


@app.post('/search')
def search_endpoint(req: SearchRequest):
    if req.bm25_weight is not None and req.vector_weight is not None:
        answer, sources, chunks = search_with_weights(req.query, req.bm25_weight, req.vector_weight)
    elif req.exclude_sources:
        answer, sources, chunks = search_filtered(req.query, req.exclude_sources, folder=req.folder)
    else:
        answer, sources, chunks = search(req.query, folder=req.folder)
    return {'answer': answer, 'sources': sources, 'chunks': chunks}


@app.post('/search/stream')
async def search_stream_endpoint(req: SearchRequest):
    return StreamingResponse(
        search_stream(req.query, folder=req.folder),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.post('/similar')
def similar_endpoint(req: SimilarRequest):
    """Return top-k similar documents by vector similarity. No LLM call."""
    results = similar(req.query, k=req.k)
    return {'results': results}


@app.post('/research')
def research_endpoint(req: ResearchRequest):
    summary, filepath = research(req.query)
    return {'summary': summary, 'filepath': filepath}
