from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from search import search, search_filtered, search_with_weights, search_stream, similar, get_stats
from research import research
from entities import EntityStore
import os
import re
from datetime import date
from pathlib import Path

app = FastAPI()

_ui_dir = os.path.join(os.path.dirname(__file__), 'ui')
app.mount('/ui', StaticFiles(directory=_ui_dir), name='ui')

_log_path = Path('/mnt/Claude/log.md')
_wiki_dir = Path('/mnt/Claude/wiki')

_entities_db = os.environ.get('ENTITIES_DB', os.path.join(os.path.dirname(__file__), 'entities.db'))
_entity_store = EntityStore(_entities_db)


class SearchRequest(BaseModel):
    query: str
    exclude_sources: list[str] = []
    bm25_weight: float | None = None
    vector_weight: float | None = None
    folder: str | None = None
    wing: str | None = None
    room: str | None = None


class SimilarRequest(BaseModel):
    query: str
    k: int = 5


class ResearchRequest(BaseModel):
    query: str


class EntityUpsertRequest(BaseModel):
    slug: str
    type: str
    name: str
    wing: str | None = None
    summary: str | None = None
    attrs: dict = {}
    aliases: list[str] = []


class WikiSaveRequest(BaseModel):
    topic: str          # slug, e.g. "openclaw-memory"
    title: str          # display title, e.g. "OpenClaw Memory System"
    answer: str         # synthesised content to save as page body
    sources: list[str] = []  # source filenames that contributed


@app.get('/')
def index():
    return FileResponse(os.path.join(_ui_dir, 'index.html'))


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/stats')
def stats():
    return get_stats()


@app.get('/entities')
def entities_list(type: str | None = None, wing: str | None = None,
                  q: str | None = None, k: int = Query(default=20, ge=1, le=200)):
    """List or search entities.

    - Pass `q=...` for FTS fuzzy search
    - Otherwise returns filtered by type/wing
    """
    if q:
        return {'entities': _entity_store.search(q, k=k)}
    return {'entities': _entity_store.list(type=type, wing=wing)}


@app.get('/entities/{slug}')
def entities_get(slug: str):
    """Fetch a single entity by slug or alias."""
    e = _entity_store.get(slug)
    if not e:
        raise HTTPException(status_code=404, detail=f'entity not found: {slug}')
    return e


@app.post('/entities')
def entities_upsert(req: EntityUpsertRequest):
    """Insert or update an entity."""
    e = _entity_store.upsert(
        slug=req.slug, type=req.type, name=req.name,
        wing=req.wing, summary=req.summary,
        attrs=req.attrs, aliases=req.aliases,
    )
    return e


@app.get('/log/recent')
def log_recent(n: int = Query(default=50, ge=1, le=500)):
    """Return the last N lines of the workspace event log. No embedding — direct tail."""
    if not _log_path.exists():
        return {'lines': [], 'path': str(_log_path), 'error': 'log.md not found'}
    text = _log_path.read_text(encoding='utf-8', errors='replace')
    all_lines = text.splitlines()
    entry_lines = [l for l in all_lines if l.startswith('[20')]
    recent = entry_lines[-n:]
    return {'lines': recent, 'total': len(entry_lines), 'returned': len(recent)}


@app.get('/wiki')
def wiki_list():
    """List all wiki pages with their metadata."""
    if not _wiki_dir.exists():
        return {'pages': []}
    pages = []
    for p in sorted(_wiki_dir.glob('*.md')):
        text = p.read_text(encoding='utf-8', errors='replace')
        # Extract frontmatter fields
        generated = ''
        title = p.stem
        if text.startswith('---'):
            end = text.find('---', 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if line.startswith('title:'):
                        title = line.partition(':')[2].strip().strip('"')
                    elif line.startswith('generated:'):
                        generated = line.partition(':')[2].strip()
        pages.append({'slug': p.stem, 'title': title, 'generated': generated, 'path': str(p)})
    return {'pages': pages}


@app.post('/wiki/save')
def wiki_save(req: WikiSaveRequest):
    """Save a synthesised answer as a wiki page in /mnt/Claude/wiki/.

    Creates or overwrites wiki/<topic>.md. The watcher picks it up and
    indexes it automatically. Intended for agents and the notes-curator
    to persist high-quality synthesis results.
    """
    _wiki_dir.mkdir(parents=True, exist_ok=True)

    # Sanitise slug — lowercase, hyphens only
    slug = re.sub(r'[^a-z0-9-]', '-', req.topic.lower().strip())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        return {'error': 'Invalid topic slug'}, 400

    path = _wiki_dir / f'{slug}.md'
    today = date.today().isoformat()
    sources_yaml = '\n'.join(f'  - {s}' for s in req.sources) if req.sources else '  []'

    # Strip a leading h1 if the answer already contains one
    body = re.sub(r'^#\s+.+\n+', '', req.answer.strip()).strip()

    content = f"""---
title: "{req.title}"
type: wiki
topic: {slug}
generated: {today}
sources:
{sources_yaml}
---

# {req.title}

{body}
"""
    path.write_text(content, encoding='utf-8')

    action = 'updated' if path.exists() else 'created'
    return {'saved': str(path), 'slug': slug, 'action': action}


@app.post('/search')
def search_endpoint(req: SearchRequest):
    if req.bm25_weight is not None and req.vector_weight is not None:
        answer, sources, chunks = search_with_weights(req.query, req.bm25_weight, req.vector_weight)
    elif req.exclude_sources:
        answer, sources, chunks = search_filtered(req.query, req.exclude_sources,
                                                  folder=req.folder, wing=req.wing, room=req.room)
    else:
        answer, sources, chunks = search(req.query, folder=req.folder, wing=req.wing, room=req.room)
    return {'answer': answer, 'sources': sources, 'chunks': chunks}


@app.post('/search/stream')
async def search_stream_endpoint(req: SearchRequest):
    return StreamingResponse(
        search_stream(req.query, folder=req.folder, wing=req.wing, room=req.room),
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
