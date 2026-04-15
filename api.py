from fastapi import FastAPI, Query, HTTPException, APIRouter
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from search import search, search_filtered, search_with_weights, search_stream, similar, get_stats
from research import research
from entities import EntityStore
import os
import re
import requests as http_requests
from datetime import date
from pathlib import Path
import threading
import time

app = FastAPI()
api = APIRouter(prefix='/api')

_ui_dir = os.path.join(os.path.dirname(__file__), 'ui')
app.mount('/ui', StaticFiles(directory=_ui_dir), name='ui')

_log_path = Path('/mnt/Claude/log.md')
_wiki_dir = Path('/mnt/Claude/wiki')
_todos_dir = Path('/mnt/Claude/todos')
_projects_dir = Path('/mnt/Claude/projects')
_inbox_dir = Path('/mnt/Obsidian/Inbox')
_notes_dir = Path('/mnt/Obsidian/Notes')
_obsidian_root = Path('/mnt/Obsidian')

def _find_note(filename: str):
    """Resolve a note filename by searching recursively under the Obsidian vault."""
    for path in _obsidian_root.rglob(filename):
        return path
    return None

_TODOS_INDEXER = 'http://192.168.88.78:3000'

_entities_db = os.environ.get('ENTITIES_DB', os.path.join(os.path.dirname(__file__), 'entities.db'))
_entity_store = EntityStore(_entities_db)


# ── Pydantic models ──────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    exclude_sources: list[str] = []
    bm25_weight: float | None = None
    vector_weight: float | None = None
    folder: str | None = None
    wing: str | None = None
    room: str | None = None
    project: str | None = None


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


class TodoCreateRequest(BaseModel):
    title: str
    description: str = ''
    priority: str = 'medium'
    swimlane: str = 'Dev'
    assignee: str = ''
    project: str = ''


class NoteCreateRequest(BaseModel):
    title: str
    description: str = ''
    project: str = ''


class NoteSearchRequest(BaseModel):
    query: str

class NoteUpdateRequest(BaseModel):
    body: str


# ── UI routes ────────────────────────────────────────────────────────────────

@app.get('/')
def home():
    return FileResponse(os.path.join(_ui_dir, 'home.html'))

@app.get('/notes')
def notes_page():
    return FileResponse(os.path.join(_ui_dir, 'notes.html'))

@app.get('/chat')
def chat_page():
    return FileResponse(os.path.join(_ui_dir, 'chat.html'))

@app.get('/search')
def search_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url='/chat', status_code=301)

@app.get('/new')
def new_page():
    return FileResponse(os.path.join(_ui_dir, 'new.html'))

@app.get('/review')
def review_page():
    return FileResponse(os.path.join(_ui_dir, 'review.html'))

@app.get('/projects')
def projects_page():
    return FileResponse(os.path.join(_ui_dir, 'projects.html'))

@app.get('/projects/{slug}')
def project_detail_page(slug: str):
    return FileResponse(os.path.join(_ui_dir, 'projects.html'))

@app.get('/manifest.json')
def manifest():
    return FileResponse(os.path.join(_ui_dir, 'manifest.json'), media_type='application/manifest+json')

@app.get('/sw.js')
def service_worker():
    return FileResponse(os.path.join(_ui_dir, 'sw.js'), media_type='application/javascript')


# ── API: health, stats ───────────────────────────────────────────────────────

@api.get('/health')
def health():
    return {'status': 'ok'}

@api.get('/stats')
def stats():
    return get_stats()


# ── API: entities ────────────────────────────────────────────────────────────

@api.get('/entities')
def entities_list(type: str | None = None, wing: str | None = None,
                  q: str | None = None, k: int = Query(default=20, ge=1, le=200)):
    if q:
        return {'entities': _entity_store.search(q, k=k)}
    return {'entities': _entity_store.list(type=type, wing=wing)}

@api.get('/entities/{slug}')
def entities_get(slug: str):
    e = _entity_store.get(slug)
    if not e:
        raise HTTPException(status_code=404, detail=f'entity not found: {slug}')
    return e

@api.post('/entities')
def entities_upsert(req: EntityUpsertRequest):
    e = _entity_store.upsert(
        slug=req.slug, type=req.type, name=req.name,
        wing=req.wing, summary=req.summary,
        attrs=req.attrs, aliases=req.aliases,
    )
    return e


# ── API: log ─────────────────────────────────────────────────────────────────

@api.get('/log/recent')
def log_recent(n: int = Query(default=50, ge=1, le=500)):
    if not _log_path.exists():
        return {'lines': [], 'path': str(_log_path), 'error': 'log.md not found'}
    text = _log_path.read_text(encoding='utf-8', errors='replace')
    all_lines = text.splitlines()
    entry_lines = [l for l in all_lines if l.startswith('[20')]
    recent = entry_lines[-n:]
    return {'lines': recent, 'total': len(entry_lines), 'returned': len(recent)}


# ── API: projects ────────────────────────────────────────────────────────────

def _parse_project_file(path: Path) -> dict | None:
    """Parse a project markdown file into a structured dict."""
    try:
        import re as _re
        text = path.read_text(encoding='utf-8', errors='replace')
        fm_match = _re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', text, _re.DOTALL)
        if not fm_match:
            return None
        import yaml as _yaml
        fm = _yaml.safe_load(fm_match.group(1)) or {}
        body = fm_match.group(2).strip()
        return {
            'slug': fm.get('slug', path.stem),
            'title': fm.get('title', path.stem),
            'status': fm.get('status', 'active'),
            'wing': fm.get('wing'),
            'room': fm.get('room'),
            'goal': fm.get('goal', ''),
            'tags': fm.get('tags', []),
            'containers': fm.get('containers', []),
            'repo': fm.get('repo'),
            'docs': fm.get('docs', []),
            'created': str(fm.get('created', '')),
            'summary': body,
        }
    except Exception:
        return None

@api.get('/projects')
def projects_list(status: str | None = None):
    if not _projects_dir.exists():
        return {'projects': []}
    projects = []
    for p in sorted(_projects_dir.glob('*.md')):
        proj = _parse_project_file(p)
        if proj is None:
            continue
        if status and proj['status'] != status:
            continue
        projects.append(proj)
    return {'projects': projects}

@api.get('/projects/{slug}')
def project_detail(slug: str):
    if not _projects_dir.exists():
        raise HTTPException(status_code=404, detail='Projects directory not found')
    for p in _projects_dir.glob('*.md'):
        proj = _parse_project_file(p)
        if proj and proj['slug'] == slug:
            return proj
    raise HTTPException(status_code=404, detail=f'Project {slug!r} not found')


# ── API: wiki ────────────────────────────────────────────────────────────────

@api.get('/wiki')
def wiki_list():
    if not _wiki_dir.exists():
        return {'pages': []}
    pages = []
    for p in sorted(_wiki_dir.glob('*.md')):
        text = p.read_text(encoding='utf-8', errors='replace')
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

@api.post('/wiki/save')
def wiki_save(req: WikiSaveRequest):
    _wiki_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^a-z0-9-]', '-', req.topic.lower().strip())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        return {'error': 'Invalid topic slug'}, 400
    path = _wiki_dir / f'{slug}.md'
    today = date.today().isoformat()
    sources_yaml = '\n'.join(f'  - {s}' for s in req.sources) if req.sources else '  []'
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


# ── API: search ───────────────────────────────────────────────────────────────

@api.post('/search')
def search_endpoint(req: SearchRequest):
    if req.bm25_weight is not None and req.vector_weight is not None:
        answer, sources, chunks = search_with_weights(req.query, req.bm25_weight, req.vector_weight)
    elif req.exclude_sources:
        answer, sources, chunks = search_filtered(req.query, req.exclude_sources,
                                                  folder=req.folder, wing=req.wing, room=req.room,
                                                  project=req.project)
    else:
        answer, sources, chunks = search(req.query, folder=req.folder, wing=req.wing, room=req.room,
                                         project=req.project)
    return {'answer': answer, 'sources': sources, 'chunks': chunks}

@api.post('/search/stream')
async def search_stream_endpoint(req: SearchRequest):
    return StreamingResponse(
        search_stream(req.query, folder=req.folder, wing=req.wing, room=req.room,
                      project=req.project),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )

@api.post('/similar')
def similar_endpoint(req: SimilarRequest):
    results = similar(req.query, k=req.k)
    return {'results': results}

@api.post('/research')
def research_endpoint(req: ResearchRequest):
    summary, filepath = research(req.query)
    return {'summary': summary, 'filepath': filepath}


# ── API: todos ────────────────────────────────────────────────────────────────

@api.get('/todos/next-id')
def todos_next_id():
    """Proxy to todos-indexer on CT 122, return the next available todo ID."""
    try:
        resp = http_requests.get(f'{_TODOS_INDEXER}/todos', timeout=5)
        resp.raise_for_status()
        todos = resp.json()
        if not todos:
            return {'next_id': 1}
        max_id = max((t.get('id', 0) for t in todos if isinstance(t.get('id'), int)), default=0)
        return {'next_id': max_id + 1}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Todos indexer unavailable: {e}')

@api.get('/todos/pending-count')
def todos_pending_count():
    """Return count of pending todos for dashboard stats."""
    try:
        resp = http_requests.get(f'{_TODOS_INDEXER}/todos?status=pending', timeout=5)
        resp.raise_for_status()
        todos = resp.json()
        return {'count': len(todos)}
    except Exception as e:
        return {'count': None, 'error': str(e)}

@api.post('/todos/create')
def todos_create(req: TodoCreateRequest):
    """Create a new todo markdown file in /mnt/Claude/todos/."""
    try:
        resp = http_requests.get(f'{_TODOS_INDEXER}/todos', timeout=5)
        resp.raise_for_status()
        existing = resp.json()
        max_id = max((t.get('id', 0) for t in existing if isinstance(t.get('id'), int)), default=0)
        todo_id = max_id + 1
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Could not determine next ID: {e}')

    slug = re.sub(r'[^a-z0-9-]', '-', req.title.lower().strip())
    slug = re.sub(r'-+', '-', slug).strip('-')[:60]
    filename = f'{todo_id:03d}-{slug}.md'
    path = _todos_dir / filename
    today = date.today().isoformat()

    assignee_line = f'assignee: {req.assignee}\n' if req.assignee else ''
    project_line = f'project: {req.project}\n' if req.project else ''
    content = (
        f'---\n'
        f'id: {todo_id}\n'
        f'title: "{req.title}"\n'
        f'status: pending\n'
        f'priority: {req.priority}\n'
        f'created: {today}\n'
        f'swimlane: {req.swimlane}\n'
        f'{assignee_line}'
        f'{project_line}'
        f'---\n'
    )
    if req.description:
        content += f'\n{req.description}\n'

    _todos_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return {'created': filename, 'id': todo_id, 'path': str(path)}

@api.post('/notes/create')
def notes_create(req: NoteCreateRequest):
    """Create a new note in /mnt/Obsidian/Notes/ with standard frontmatter."""
    slug = re.sub(r'[^a-z0-9-]', '-', req.title.lower().strip())
    slug = re.sub(r'-+', '-', slug).strip('-')[:80]
    filename = f'{slug}.md'
    path = Path('/mnt/Obsidian/Notes') / filename
    today = date.today().isoformat()

    note_project_line = f'project: {req.project}\n' if req.project else ''
    content = (
        f'---\n'
        f'title: "{req.title}"\n'
        f'date_created: {today}\n'
        f'reviewed: unreviewed\n'
        f'tags: []\n'
        f'{note_project_line}'
        f'---\n'
    )
    if req.description:
        content += f'\n{req.description}\n'

    tmp = path.parent / f'.tmp_{os.getpid()}_{filename}'
    try:
        tmp.write_text(content, encoding='utf-8')
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {'created': filename, 'path': str(path)}


# ── API: notes (search, fetch, update) ───────────────────────────────────────

@api.post('/notes/search')
def notes_search(req: NoteSearchRequest):
    """Keyword+vector search returning deduplicated note-level results."""
    _, _, chunks = search(req.query)
    note_map: dict[str, dict] = {}
    for chunk in chunks:
        src = chunk['source']
        if src not in note_map or chunk['score'] > note_map[src]['score']:
            note_map[src] = {
                'filename': src,
                'snippet': chunk['content'][:200],
                'score': chunk['score'],
            }
    results = sorted(note_map.values(), key=lambda x: x['score'], reverse=True)
    # Only keep results resolvable under the Obsidian vault
    resolved = []
    for r in results:
        path = _find_note(r['filename'])
        if path is None:
            continue
        resolved.append(r)
        r['_path'] = path
    results = resolved
    for r in results:
        path = r.pop('_path')
        title = r['filename'].removesuffix('.md').replace('-', ' ').title()
        if path.exists():
            text = path.read_text(encoding='utf-8', errors='replace')
            if text.startswith('---'):
                end = text.find('---', 3)
                if end != -1:
                    for line in text[3:end].splitlines():
                        if line.startswith('title:'):
                            title = line.partition(':')[2].strip().strip('"\'')
                            break
        r['title'] = title
    return {'results': results}


@api.get('/notes/{filename:path}')
def notes_get(filename: str):
    """Fetch a note's title and body (frontmatter stripped)."""
    filename = os.path.basename(filename)
    if not filename.endswith('.md'):
        raise HTTPException(status_code=400, detail='Only .md files are supported')
    path = _find_note(filename)
    if path is None:
        raise HTTPException(status_code=404, detail=f'Note not found: {filename}')
    content = path.read_text(encoding='utf-8', errors='replace')
    title = filename.removesuffix('.md').replace('-', ' ').title()
    body = content
    if content.startswith('---'):
        end = content.find('---', 3)
        if end != -1:
            for line in content[3:end].splitlines():
                if line.startswith('title:'):
                    title = line.partition(':')[2].strip().strip('"\'')
            body = content[end + 3:].lstrip('\n')
    return {'filename': filename, 'title': title, 'body': body}


@api.patch('/notes/{filename:path}')
def notes_update(filename: str, req: NoteUpdateRequest):
    """Save edited note body; sets reviewed: unreviewed and adds/updates updated: date."""
    filename = os.path.basename(filename)
    if not filename.endswith('.md'):
        raise HTTPException(status_code=400, detail='Only .md files are supported')
    path = _find_note(filename)
    if path is None:
        raise HTTPException(status_code=404, detail=f'Note not found: {filename}')
    today = date.today().isoformat()
    content = path.read_text(encoding='utf-8', errors='replace')
    if content.startswith('---'):
        end = content.find('---', 3)
        if end != -1:
            fm_lines = content[3:end].splitlines()
            new_fm: list[str] = []
            has_reviewed = has_updated = False
            for line in fm_lines:
                if line.startswith('reviewed:'):
                    new_fm.append('reviewed: unreviewed')
                    has_reviewed = True
                elif line.startswith('updated:'):
                    new_fm.append(f'updated: {today}')
                    has_updated = True
                else:
                    new_fm.append(line)
            if not has_reviewed:
                new_fm.append('reviewed: unreviewed')
            if not has_updated:
                new_fm.append(f'updated: {today}')
            new_content = '---\n' + '\n'.join(new_fm) + '\n---\n\n' + req.body.strip() + '\n'
        else:
            new_content = req.body
    else:
        new_content = req.body
    tmp = path.parent / f'.tmp_{os.getpid()}_{filename}'
    try:
        tmp.write_text(new_content, encoding='utf-8')
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {'saved': filename, 'updated': today}


# ── Discord notifications ─────────────────────────────────────────────────────

def _discord_notify(message: str):
    url = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not url:
        return
    try:
        http_requests.post(url, json={'content': message}, timeout=5)
    except Exception as e:
        print(f'[discord] notify failed: {e}', flush=True)


def _watch_queue_completion(filepath, label: str, topic: str, poll_interval: int = 15, timeout: int = 3600):
    """Background thread: poll until queue file is gone, then notify Discord."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        if not filepath.exists():
            _discord_notify(f'{label} complete: **{topic}**\nReport saved to Obsidian Inbox.')
            return
    print(f'[discord] watcher timed out for {filepath.name}', flush=True)

# ── API: research queue ───────────────────────────────────────────────────────

_queue_dir = Path('/mnt/Claude/research-queue')
_inbox_dir_path = Path('/mnt/Obsidian/Inbox')


class StashCreateRequest(BaseModel):
    url: str
    description: str = ''


class ResearchCreateRequest(BaseModel):
    prompt: str


class ForecastCreateRequest(BaseModel):
    prompt: str


def _queue_slug(text: str, max_len: int = 50) -> str:
    slug = re.sub(r'https?://(www\.)?', '', text.lower())
    slug = re.sub(r'[^a-z0-9-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug[:max_len]


@api.post('/queue/stash')
def queue_stash(req: StashCreateRequest):
    """Write a stash queue file to /mnt/Claude/research-queue/."""
    from datetime import datetime, timezone
    slug = _queue_slug(req.url)
    filename = f'stash-{slug}.md'
    path = _queue_dir / filename
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M+00:00')
    content = (
        f'---\n'
        f'type: stash\n'
        f'url: {req.url}\n'
        f'requested_by: Angelo\n'
        f'requested_at: {now}\n'
        f'---\n'
    )
    if req.description:
        content += f'\n{req.description}\n'
    _queue_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return {'created': filename}


@api.post('/queue/research')
def queue_research(req: ResearchCreateRequest):
    """Write a research queue file to /mnt/Claude/research-queue/."""
    from datetime import datetime, timezone
    slug = _queue_slug(req.prompt)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename = f'research-{ts}-{slug}.md'
    path = _queue_dir / filename
    content = (
        f'---\n'
        f'type: research\n'
        f'topic: {req.prompt}\n'
        f'---\n'
    )
    _queue_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    threading.Thread(target=_watch_queue_completion, args=(path, 'Research', req.prompt), daemon=True).start()
    return {'created': filename}


@api.post('/queue/forecast')
def queue_forecast(req: ForecastCreateRequest):
    """Write a forecast queue file to /mnt/Claude/research-queue/."""
    from datetime import datetime, timezone
    slug = _queue_slug(req.prompt)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename = f'forecast-{ts}-{slug}.md'
    path = _queue_dir / filename
    content = (
        f'---\n'
        f'type: forecast\n'
        f'topic: {req.prompt}\n'
        f'---\n'
    )
    _queue_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    threading.Thread(target=_watch_queue_completion, args=(path, 'Forecast', req.prompt), daemon=True).start()
    return {'created': filename}


@api.get('/queue/stash')
def queue_stash_list():
    """List pending stash queue files (unprocessed URLs)."""
    if not _queue_dir.exists():
        return {'items': []}
    items = []
    for p in sorted(_queue_dir.glob('stash-*.md')):
        text = p.read_text(encoding='utf-8', errors='replace')
        url = ''
        requested_at = ''
        if text.startswith('---'):
            end = text.find('---', 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if line.startswith('url:'):
                        url = line.partition(':')[2].strip()
                    elif line.startswith('requested_at:'):
                        requested_at = line.partition(':')[2].strip()
        items.append({'filename': p.name, 'url': url, 'requested_at': requested_at})
    return {'items': items}


@api.get('/queue/status/{filename}')
def queue_status(filename: str):
    """
    Check the status of a queued job by filename.
    - queued: file still exists in research-queue/
    - done: file is gone (picked up by Hermes)
    """
    path = _queue_dir / filename
    if path.exists():
        return {'status': 'queued', 'filename': filename}
    return {'status': 'done', 'filename': filename}


# ── API: review ───────────────────────────────────────────────────────────────

import json
from review import (
    scan_unreviewed, parse_frontmatter, write_frontmatter,
    group_notes, SessionManager, generate_question, infer_tags,
    build_review_content,
)

_session_mgr = SessionManager()
_review_notes_dir = '/mnt/Obsidian/Notes'


class ReviewStartRequest(BaseModel):
    note_ids: list[str]


class ReviewReplyRequest(BaseModel):
    answer: str
    question: str = ''


@api.get('/review/queue')
def review_queue():
    notes = scan_unreviewed(_review_notes_dir)
    if not notes:
        return {'notes': [], 'groups': []}
    similarity = {}
    if len(notes) > 1:
        for i, a in enumerate(notes):
            for b in notes[i + 1:]:
                try:
                    results = similar(a['preview'], k=5)
                    b_score = 0.0
                    for r in results:
                        if b['filename'] in r.get('source', ''):
                            b_score = 0.65
                            break
                    similarity[(a['filename'], b['filename'])] = b_score
                except Exception:
                    similarity[(a['filename'], b['filename'])] = 0.0
    groups = group_notes(notes, similarity, threshold=0.4)
    return {'notes': notes, 'groups': groups}

@api.post('/review/start')
async def review_start(req: ReviewStartRequest):
    notes_data = []
    for fname in req.note_ids:
        path = os.path.join(_review_notes_dir, fname)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f'Note not found: {fname}')
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        fm, body = parse_frontmatter(content)
        previous_reviews = []
        for m in re.finditer(r'\*\*(.+?)\*\*\s+(.+)', body):
            previous_reviews.append({'q': m.group(1), 'a': m.group(2)})
        notes_data.append({
            'filename': fname,
            'path': path,
            'body': body,
            'review_count': fm.get('review_count', 0),
            'previous_reviews': previous_reviews,
        })
    combined_text = ' '.join(n['body'][:200] for n in notes_data)
    rag_context = ''
    try:
        _, _, chunks = search(combined_text)
        rag_context = '\n'.join(f"[{c['source']}] {c['content'][:200]}" for c in chunks[:3])
    except Exception:
        pass
    session = _session_mgr.create(req.note_ids, notes_data)
    session['rag_context'] = rag_context
    all_previous = []
    for n in notes_data:
        all_previous.extend(n.get('previous_reviews', []))

    async def stream():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session['session_id'], 'notes': [{'filename': n['filename'], 'body': n['body']} for n in notes_data]})}\n\n"
        question_text = ''
        async for token in generate_question(notes_data, rag_context, all_previous, [], 0):
            question_text += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
        session['pending_question'] = question_text
        yield f"data: {json.dumps({'type': 'question_done', 'full_question': question_text})}\n\n"

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@api.post('/review/{session_id}/reply')
async def review_reply(session_id: str, req: ReviewReplyRequest):
    session = _session_mgr.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail='Session not found')
    question = req.question or session.get('pending_question', '')
    _session_mgr.add_qa(session_id, question, req.answer)
    if session['question_count'] >= 3:
        async def stream_done():
            yield f"data: {json.dumps({'type': 'interview_done'})}\n\n"
        return StreamingResponse(stream_done(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    all_previous = []
    for n in session['notes']:
        all_previous.extend(n.get('previous_reviews', []))

    async def stream():
        question_text = ''
        async for token in generate_question(
            session['notes'], session.get('rag_context', ''),
            all_previous, session['qa'], session['question_count'],
        ):
            question_text += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
        session['pending_question'] = question_text
        yield f"data: {json.dumps({'type': 'question_done', 'full_question': question_text})}\n\n"

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@api.post('/review/{session_id}/complete')
async def review_complete(session_id: str):
    session = _session_mgr.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail='Session not found')
    results = []
    for note in session['notes']:
        path = note['path']
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            results.append({'filename': note['filename'], 'success': False, 'error': str(e)})
            continue
        fm, body = parse_frontmatter(content)
        review_num = fm.get('review_count', 0) + 1
        tags = await infer_tags([note], session.get('rag_context', ''), session['qa'])
        review_content = build_review_content(session['qa'])
        new_content = write_frontmatter(fm, body, tags, review_num, review_content)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except OSError as e:
            results.append({'filename': note['filename'], 'success': False, 'error': str(e)})
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                verify_fm, _ = parse_frontmatter(f.read())
            if verify_fm.get('reviewed') is not True:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                with open(path, 'r', encoding='utf-8') as f:
                    verify_fm, _ = parse_frontmatter(f.read())
                if verify_fm.get('reviewed') is not True:
                    results.append({'filename': note['filename'], 'success': False,
                                    'error': 'Write verification failed after retry'})
                    continue
        except OSError as e:
            results.append({'filename': note['filename'], 'success': False, 'error': str(e)})
            continue
        results.append({'filename': note['filename'], 'success': True,
                        'tags': tags, 'review_num': review_num})
    _session_mgr.remove(session_id)
    return {'results': results}

@api.post('/review/{note_id:path}/skip')
def review_skip(note_id: str):
    return {'skipped': note_id}

@api.post('/review/{note_id:path}/auto-tag')
async def review_auto_tag(note_id: str):
    path = os.path.join(_review_notes_dir, note_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f'Note not found: {note_id}')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    fm, body = parse_frontmatter(content)
    review_num = fm.get('review_count', 0) + 1
    rag_context = ''
    try:
        _, _, chunks = search(body[:200])
        rag_context = '\n'.join(f"[{c['source']}] {c['content'][:200]}" for c in chunks[:3])
    except Exception:
        pass
    tags = await infer_tags([{'filename': note_id, 'body': body}], rag_context, [])
    review_content = build_review_content([])
    new_content = write_frontmatter(fm, body, tags, review_num, review_content)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    with open(path, 'r', encoding='utf-8') as f:
        verify_fm, _ = parse_frontmatter(f.read())
    if verify_fm.get('reviewed') is not True:
        return {'success': False, 'error': 'Write verification failed'}
    return {'success': True, 'tags': tags, 'filename': note_id, 'review_num': review_num}


# ── API: services health ──────────────────────────────────────────────────────

_SERVICES = [
    {'id': 'notes',    'label': 'Notes RAG',  'url': 'http://192.168.88.71:8080/api/health'},
    {'id': 'kanban',   'label': 'Kanban',      'url': 'http://192.168.88.78:3000'},
    {'id': 'reports',  'label': 'Reports',     'url': 'http://192.168.88.55:80'},
    {'id': 'grafana',  'label': 'Grafana',     'url': 'http://192.168.88.73:3000'},
    {'id': 'dashboard','label': 'Dashboard',   'url': 'http://192.168.88.127:8080'},
]

@api.get('/services/health')
def services_health():
    """Server-side health check for all linked services."""
    results = {}
    for svc in _SERVICES:
        try:
            r = http_requests.get(svc['url'], timeout=2)
            results[svc['id']] = 'up' if r.status_code < 500 else 'down'
        except Exception:
            results[svc['id']] = 'down'
    return results



# ── API: LLM models ────────────────────────────────────────────────────────────

_models_config_path = Path('/mnt/Claude/config/models.json')
_model_health_path  = Path('/mnt/Claude/config/model-health.json')

_SIDECAR_URLS = {
    'hermes':        'http://192.168.88.83:8090/reload',
    'mirofish':      'http://192.168.88.79:8091/reload',
    'openclaw':      'http://192.168.88.63:8092/reload',
    'notes-curator': 'http://192.168.88.63:8092/reload',
}
_DISCORD_SERVICES = frozenset({'hermes', 'openclaw', 'notes-curator'})
_ALL_SERVICES     = ['notes-rag', 'hermes', 'mirofish', 'openclaw', 'notes-curator']
_SERVICE_LABELS   = {
    'notes-rag':     'Notes RAG',
    'hermes':        'Hermes',
    'mirofish':      'MiroFish',
    'openclaw':      'OpenClaw',
    'notes-curator': 'Notes Curator',
}


class ModelUpdateRequest(BaseModel):
    model: str


@api.get('/models')
def models_list():
    models = json.loads(_models_config_path.read_text()) if _models_config_path.exists() else {}
    health = json.loads(_model_health_path.read_text()) if _model_health_path.exists() else {}
    return {
        'services': [
            {
                'id':              svc,
                'label':           _SERVICE_LABELS.get(svc, svc),
                'model':           models.get(svc, ''),
                'health':          health.get(svc, {'status': 'unknown'}),
                'discord_warning': svc in _DISCORD_SERVICES,
            }
            for svc in _ALL_SERVICES
        ]
    }


@api.get('/models/validate')
def models_validate(model: str = Query(...)):
    model_id = re.sub(r'^openrouter/', '', model.strip())
    try:
        r = http_requests.get(
            'https://openrouter.ai/api/v1/models',
            headers={'Authorization': f'Bearer {os.environ.get("OPENROUTER_API_KEY", "")}'},
            timeout=8,
        )
        r.raise_for_status()
        ids = {m['id'] for m in r.json().get('data', [])}
        return {'valid': model_id in ids, 'model': model_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'OpenRouter unreachable: {e}')


@api.post('/models/{service}')
def models_update(service: str, req: ModelUpdateRequest):
    if service not in _ALL_SERVICES:
        raise HTTPException(status_code=400, detail=f'Unknown service: {service}')
    _models_config_path.parent.mkdir(parents=True, exist_ok=True)
    models = json.loads(_models_config_path.read_text()) if _models_config_path.exists() else {}
    models[service] = req.model
    _models_config_path.write_text(json.dumps(models, indent=2))
    if service in _SIDECAR_URLS:
        try:
            r = http_requests.post(_SIDECAR_URLS[service], json={'service': service}, timeout=30)
            r.raise_for_status()
            return {'updated': service, 'model': req.model, 'reloaded': True}
        except Exception as e:
            return {'updated': service, 'model': req.model, 'reloaded': False, 'reload_error': str(e)}
    return {'updated': service, 'model': req.model, 'reloaded': True}


# ── Register router + backward-compat aliases ─────────────────────────────────
app.include_router(api)

# Old paths kept as aliases so external callers (OpenClaw, skills) keep working
# during the transition — remove once AGENTS.md + SKILL.md are updated and verified.
app.add_api_route('/health',            health,                 methods=['GET'])
app.add_api_route('/stats',             stats,                  methods=['GET'])
app.add_api_route('/search',            search_endpoint,        methods=['POST'])
app.add_api_route('/search/stream',     search_stream_endpoint, methods=['POST'])
app.add_api_route('/similar',           similar_endpoint,       methods=['POST'])
app.add_api_route('/research',          research_endpoint,      methods=['POST'])
app.add_api_route('/wiki',              wiki_list,              methods=['GET'])
app.add_api_route('/wiki/save',         wiki_save,              methods=['POST'])
app.add_api_route('/log/recent',        log_recent,             methods=['GET'])
app.add_api_route('/entities',          entities_list,          methods=['GET'])
app.add_api_route('/entities',          entities_upsert,        methods=['POST'])
app.add_api_route('/review/queue',      review_queue,           methods=['GET'])
app.add_api_route('/review/start',      review_start,           methods=['POST'])
app.add_api_route('/review/{session_id}/reply',      review_reply,      methods=['POST'])
app.add_api_route('/review/{session_id}/complete',   review_complete,   methods=['POST'])
app.add_api_route('/review/{note_id:path}/skip',     review_skip,       methods=['POST'])
app.add_api_route('/review/{note_id:path}/auto-tag', review_auto_tag,   methods=['POST'])
