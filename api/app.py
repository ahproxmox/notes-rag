from fastapi import FastAPI, Query, HTTPException, APIRouter, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from core.search import search, search_filtered, search_with_weights, search_stream, similar, get_stats, retrieve_hybrid
from features.research import research
from features.entities import EntityStore
from features import links
import os
import re
import sqlite3
import requests as http_requests
from datetime import date, datetime
from pathlib import Path
import threading
import time
from infra import caldav_bridge

app = FastAPI()
api = APIRouter(prefix='/api')

_ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui')
app.mount('/ui', StaticFiles(directory=_ui_dir), name='ui')

_log_path = Path('/mnt/Claude/log.md')
_wiki_dir = Path('/mnt/Claude/wiki')
_todos_dir = Path('/mnt/Claude/todos')
_reminders_db = Path(os.path.dirname(os.path.dirname(__file__))) / 'reminders.db'
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

_entities_db = os.environ.get('ENTITIES_DB', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'entities.db'))
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
    include_superseded: bool = False


class SimilarRequest(BaseModel):
    query: str
    k: int = 5


class LinkScanRequest(BaseModel):
    path: str


class LinkConfirmRequest(BaseModel):
    type: str  # "supersedes" | "related"
    source: str
    target: str


class LinkRejectRequest(BaseModel):
    source: str
    target: str


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
    complexity: str = ''
    due: str = ''
    create_reminder: bool = False


class NoteCreateRequest(BaseModel):
    title: str
    description: str = ''
    project: str = ''


class NoteSearchRequest(BaseModel):
    query: str

class NoteUpdateRequest(BaseModel):
    body: str
    project: str | None = None

class PaperlessIngestRequest(BaseModel):
    paperless_id: int
    title: str
    content: str
    correspondent: str = ''
    doc_type: str = ''
    tags: list[str] = []
    created: str = ''

class ProjectCreateRequest(BaseModel):
    title: str
    goal: str = ''
    status: str = 'active'
    wing: str = ''
    tags: list[str] = []



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


@app.get('/kanban')
def kanban_page():
    return FileResponse(os.path.join(_ui_dir, 'kanban.html'))

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
        try:
            import mistune as _mistune
            summary_html = _mistune.html(body) if body else ''
        except Exception:
            summary_html = ''
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
            'summary_html': summary_html,
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


def _discover_related_docs(slug: str, title: str) -> list[dict]:
    """Auto-discover sessions, plans, and Obsidian notes related to a project."""
    import yaml as _yaml
    related = []
    slug_words = set(re.split(r'[-_]', slug.lower())) - {'the', 'a', 'an', 'and', 'or'}
    title_words = set(re.split(r'\W+', title.lower())) - {'the', 'a', 'an', 'and', 'or', ''}

    def _fm(text):
        m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
        if not m:
            return {}
        try:
            return _yaml.safe_load(m.group(1)) or {}
        except Exception:
            return {}

    def _slug_in_filename(stem):
        # strip date prefix YYYY-MM-DD-HH-
        topic = re.sub(r'^\d{4}-\d{2}-\d{2}-\d{2}-', '', stem)
        topic_words = set(re.split(r'[-_]', topic.lower()))
        return bool(slug_words & topic_words)

    # Sessions
    sessions_dir = _workspace_root / 'sessions'
    if sessions_dir.exists():
        for p in sorted(sessions_dir.glob('*.md'), reverse=True):
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
                fm = _fm(text)
                # Match by explicit project field or slug words in filename
                proj_field = str(fm.get('project', '')).lower()
                if proj_field == slug or _slug_in_filename(p.stem):
                    related.append({
                        'label': f'Session: {p.stem}',
                        'path': f'sessions/{p.name}',
                        'source': 'session',
                    })
            except Exception:
                pass

    # Plans
    plans_dir = _workspace_root / 'plans'
    if plans_dir.exists():
        for p in sorted(plans_dir.glob('*.md')):
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
                fm = _fm(text)
                proj_field = str(fm.get('project', '')).lower()
                preview = text[:400].lower()
                if proj_field == slug or slug in preview or bool(slug_words & set(re.split(r'\W+', preview))):
                    label = fm.get('title') or p.stem.replace('-', ' ').title()
                    related.append({
                        'label': f'Plan: {label}',
                        'path': f'plans/{p.name}',
                        'source': 'plan',
                    })
            except Exception:
                pass

    # Obsidian Notes
    notes_dir = Path('/mnt/Obsidian/Notes')
    if notes_dir.exists():
        for p in sorted(notes_dir.glob('*.md')):
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
                fm = _fm(text)
                tags = [str(t).lower() for t in (fm.get('tags') or [])]
                proj_field = str(fm.get('project', '')).lower()
                tag_words = set(' '.join(tags).replace('-', ' ').split())
                if proj_field == slug or bool(slug_words & tag_words) or bool(title_words & tag_words):
                    related.append({
                        'label': f'Note: {p.stem}',
                        'path': f'obsidian/Notes/{p.name}',
                        'source': 'note',
                    })
            except Exception:
                pass

    return related


@api.get('/projects/{slug}')
def project_detail(slug: str):
    if not _projects_dir.exists():
        raise HTTPException(status_code=404, detail='Projects directory not found')
    for p in _projects_dir.glob('*.md'):
        proj = _parse_project_file(p)
        if proj and proj['slug'] == slug:
            proj['related_docs'] = _discover_related_docs(slug, proj.get('title', slug))
            return proj
    raise HTTPException(status_code=404, detail=f'Project {slug!r} not found')


@api.post('/projects')
def project_create(req: ProjectCreateRequest):
    import json as _json, re as _re
    if not req.title.strip():
        raise HTTPException(status_code=400, detail='Title is required')
    if req.status not in {'active', 'paused', 'complete', 'archived'}:
        raise HTTPException(status_code=400, detail='Invalid status')
    slug = _re.sub(r'[^a-z0-9]+', '-', req.title.strip().lower()).strip('-')
    _projects_dir.mkdir(parents=True, exist_ok=True)
    target = _projects_dir / f'{slug}.md'
    if target.exists():
        i = 2
        while (_projects_dir / f'{slug}-{i}.md').exists():
            i += 1
        slug = f'{slug}-{i}'
        target = _projects_dir / f'{slug}.md'
    tags_yaml = _json.dumps(req.tags)
    goal_safe = req.goal.strip().replace('"', '\\"')
    title_safe = req.title.strip().replace('"', '\\"')
    lines = ['---', f'title: "{title_safe}"', f'slug: {slug}', f'status: {req.status}']
    if req.wing.strip():
        lines.append(f'wing: {req.wing.strip()}')
    lines += [f'goal: "{goal_safe}"', f'tags: {tags_yaml}', f'created: {date.today().isoformat()}', '---', '']
    target.write_text('\n'.join(lines), encoding='utf-8')
    return _parse_project_file(target)


@api.patch('/projects/{slug}')
async def project_update(slug: str, request: Request):
    import json as _json
    updates = _json.loads(await request.body())
    if not _projects_dir.exists():
        raise HTTPException(status_code=404, detail='Projects directory not found')
    target = None
    for p in _projects_dir.glob('*.md'):
        proj = _parse_project_file(p)
        if proj and proj['slug'] == slug:
            target = p
            break
    if not target:
        raise HTTPException(status_code=404, detail=f'Project {slug!r} not found')
    content = target.read_text(encoding='utf-8')
    if 'status' in updates:
        if updates['status'] not in {'active', 'paused', 'complete', 'archived'}:
            raise HTTPException(status_code=400, detail='Invalid status')
        content = _set_fm_field(content, 'status', updates['status'])
    if 'title' in updates:
        safe = str(updates['title']).strip().replace('"', '\\"')
        content = _set_fm_field(content, 'title', f'"{safe}"')
    if 'goal' in updates:
        safe = str(updates['goal']).strip().replace('"', '\\"')
        content = _set_fm_field(content, 'goal', f'"{safe}"')
    if 'wing' in updates:
        content = _set_fm_field(content, 'wing', str(updates['wing']).strip())
    if 'tags' in updates and isinstance(updates['tags'], list):
        content = _set_fm_field(content, 'tags', _json.dumps(updates['tags']))
    if 'summary' in updates and isinstance(updates['summary'], str):
        fm_end = content.find('---', 3)
        if fm_end != -1:
            content = content[:fm_end + 3] + '\n' + updates['summary'].strip() + '\n'
        else:
            content = updates['summary'].strip() + '\n'
    target.write_text(content, encoding='utf-8')
    return _parse_project_file(target)


# ── API: workspace file reader ───────────────────────────────────────────────

_workspace_root = Path('/mnt/Claude')
_obsidian_notes_root = Path('/mnt/Obsidian')

@api.get('/workspace/{path:path}')
def workspace_file(path: str):
    """Serve a read-only file by relative path.
    Paths starting with 'obsidian/' are resolved under /mnt/Obsidian/,
    all others under /mnt/Claude/.
    """
    try:
        if path.startswith('obsidian/'):
            rel = path[len('obsidian/'):]
            root = _obsidian_notes_root
            target = (root / rel).resolve()
            target.relative_to(root.resolve())
        else:
            root = _workspace_root
            target = (root / path).resolve()
            target.relative_to(root.resolve())
    except (ValueError, Exception):
        raise HTTPException(status_code=400, detail='Invalid path')
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail='File not found')
    return {'path': path, 'content': target.read_text(encoding='utf-8', errors='replace')}



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
        answer, sources, chunks = search_with_weights(req.query, req.bm25_weight, req.vector_weight,
                                                      include_superseded=req.include_superseded)
    elif req.exclude_sources:
        answer, sources, chunks = search_filtered(req.query, req.exclude_sources,
                                                  folder=req.folder, wing=req.wing, room=req.room,
                                                  project=req.project,
                                                  include_superseded=req.include_superseded)
    else:
        answer, sources, chunks = search(req.query, folder=req.folder, wing=req.wing, room=req.room,
                                         project=req.project,
                                         include_superseded=req.include_superseded)
    return {'answer': answer, 'sources': sources, 'chunks': chunks}

@api.post('/search/stream')
async def search_stream_endpoint(req: SearchRequest):
    return StreamingResponse(
        search_stream(req.query, folder=req.folder, wing=req.wing, room=req.room,
                      project=req.project, include_superseded=req.include_superseded),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )

@api.post('/similar')
def similar_endpoint(req: SimilarRequest):
    results = similar(req.query, k=req.k)
    return {'results': results}



def _reindex_paths(paths):
    """Fire-and-forget reindex of a small list of files. Failures are logged only."""
    try:
        from core.indexer import index_file
        for p in paths:
            try:
                index_file(p)
            except Exception as e:
                print(f'[links] reindex {p} failed: {e}', flush=True)
    except Exception as e:
        print(f'[links] reindex worker crashed: {e}', flush=True)


@api.post('/links/scan')
def links_scan(req: LinkScanRequest):
    filename = os.path.basename(req.path)
    path = _find_note(filename)
    if path is None:
        raise HTTPException(status_code=404, detail=f'Note not found: {filename}')
    from features.review import _get_review_llm
    llm = _get_review_llm()
    def resolve(f: str):
        return _find_note(os.path.basename(f))
    try:
        results = links.scan_links(path, resolve, retrieve_hybrid, llm)
    except Exception as e:
        print(f'[links] scan failed for {filename}: {e}', flush=True)
        raise HTTPException(status_code=500, detail=f'Scan failed: {e}')
    return results


@api.post('/links/confirm')
def links_confirm(req: LinkConfirmRequest):
    src_path = _find_note(os.path.basename(req.source))
    tgt_path = _find_note(os.path.basename(req.target))
    if src_path is None:
        raise HTTPException(status_code=404, detail=f'Source not found: {req.source}')
    if tgt_path is None:
        raise HTTPException(status_code=404, detail=f'Target not found: {req.target}')
    if req.type == 'supersedes':
        try:
            src_fm, tgt_fm = links.commit_supersedes(src_path, tgt_path)
        except links.ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    elif req.type == 'related':
        src_fm, tgt_fm = links.commit_related(src_path, tgt_path)
    else:
        raise HTTPException(status_code=400, detail=f'Unknown type: {req.type}')
    threading.Thread(target=_reindex_paths, args=([src_path, tgt_path],), daemon=True).start()
    # yaml-safe serialisation: convert date objects for JSON response
    def _jsonable(fm):
        out = {}
        for k, v in fm.items():
            out[k] = v.isoformat() if hasattr(v, 'isoformat') else v
        return out
    return {'source': req.source, 'target': req.target,
            'source_fm': _jsonable(src_fm), 'target_fm': _jsonable(tgt_fm)}


@api.post('/links/reject')
def links_reject(req: LinkRejectRequest):
    src_path = _find_note(os.path.basename(req.source))
    if src_path is None:
        raise HTTPException(status_code=404, detail=f'Source not found: {req.source}')
    links.reject_candidate(src_path, os.path.basename(req.target))
    return {'source': req.source, 'target': req.target}

@api.post('/research')
def research_endpoint(req: ResearchRequest):
    summary, filepath = research(req.query)
    return {'summary': summary, 'filepath': filepath}


class ReminderAckRequest(BaseModel):
    id: int

class ReminderCompleteRequest(BaseModel):
    todo_id: str

# ── API: reminders ───────────────────────────────────────────────────────────

@api.get('/reminders/queue')
def reminders_queue():
    """Pending reminders waiting to be created by the Mac bridge."""
    conn = sqlite3.connect(_reminders_db)
    rows = conn.execute(
        'SELECT id, todo_id, title, due_iso, notes FROM queue WHERE status = "pending" ORDER BY id'
    ).fetchall()
    conn.close()
    return [{'id': r[0], 'todo_id': r[1], 'title': r[2], 'due_iso': r[3], 'notes': r[4]} for r in rows]

@api.get('/reminders/tracked')
def reminders_tracked():
    """Reminders that have been created on Mac and are being watched for completion."""
    conn = sqlite3.connect(_reminders_db)
    rows = conn.execute(
        'SELECT id, todo_id, title FROM queue WHERE status = "acked" ORDER BY id'
    ).fetchall()
    conn.close()
    return [{'id': r[0], 'todo_id': r[1], 'title': r[2]} for r in rows]

@api.post('/reminders/ack')
def reminders_ack(req: ReminderAckRequest):
    """Mark a queued reminder as created (acked) by the Mac bridge."""
    conn = sqlite3.connect(_reminders_db)
    conn.execute('UPDATE queue SET status = "acked" WHERE id = ?', (req.id,))
    conn.commit()
    conn.close()
    return {'ok': True}

@api.post('/reminders/complete')
def reminders_complete(req: ReminderCompleteRequest):
    """Called by Mac bridge when a reminder is ticked off in Reminders app."""
    todo_id = str(req.todo_id).zfill(3)
    for path in _todos_dir.glob(f'{todo_id}-*.md'):
        text = path.read_text(encoding='utf-8')
        if 'status: pending' in text:
            path.write_text(text.replace('status: pending', 'status: completed', 1), encoding='utf-8')
        break
    conn = sqlite3.connect(_reminders_db)
    conn.execute('UPDATE queue SET status = "completed" WHERE todo_id = ? AND status = "acked"', (str(req.todo_id),))
    conn.commit()
    conn.close()
    return {'ok': True}

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

@api.get('/todos/by-project/{slug}')
def todos_by_project(slug: str):
    """Return todos tagged with this project slug."""
    try:
        resp = http_requests.get(f'{_TODOS_INDEXER}/todos', timeout=5)
        resp.raise_for_status()
        todos = resp.json()
        filtered = [t for t in todos if str(t.get('project', '')).lower() == slug.lower()]
        return {'todos': filtered}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Todos indexer unavailable: {e}')

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

    assignee_line   = f'assignee: {req.assignee}\n' if req.assignee else ''
    project_line    = f'project: {req.project}\n' if req.project else ''
    valid_complexities = {'small', 'medium', 'large'}
    complexity_line = f'complexity: {req.complexity}\n' if req.complexity in valid_complexities else ''
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
        f'{complexity_line}'
        f'---\n'
    )
    if req.description:
        content += f'\n{req.description}\n'

    _todos_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')

    if req.create_reminder:
        due_dt = None
        if req.due:
            from zoneinfo import ZoneInfo
            try:
                due_dt = datetime.fromisoformat(req.due).replace(tzinfo=ZoneInfo('Australia/Melbourne'))
            except ValueError:
                pass
        caldav_bridge.create_reminder(
            title=req.title,
            todo_id=todo_id,
            due_dt=due_dt,
            notes=req.description[:200] if req.description else '',
        )

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


@api.get('/notes/recent')
def notes_recent(n: int = Query(default=10, ge=1, le=50)):
    """Return the n most recently modified notes from the Obsidian vault."""
    notes_dir = Path('/mnt/Obsidian/Notes')
    results = []
    if notes_dir.is_dir():
        for p in notes_dir.glob('*.md'):
            try:
                mtime = p.stat().st_mtime
                content = p.read_text(encoding='utf-8', errors='replace')
                title = p.stem.replace('-', ' ').title()
                snippet = ''
                if content.startswith('---'):
                    end = content.find('---', 3)
                    if end != -1:
                        for line in content[3:end].splitlines():
                            if line.startswith('title:'):
                                title = line.partition(':')[2].strip().strip('"\'')
                        body = content[end + 3:].lstrip('\n')
                        body_lines = [l.strip() for l in body.splitlines()
                                      if l.strip() and not l.strip().startswith('#')]
                        snippet = ' '.join(body_lines[:3])[:200]
                else:
                    body_lines = [l.strip() for l in content.splitlines()
                                  if l.strip() and not l.strip().startswith('#')]
                    snippet = ' '.join(body_lines[:3])[:200]
                results.append({'filename': p.name, 'title': title, 'snippet': snippet, 'mtime': mtime})
            except OSError:
                continue
    results.sort(key=lambda r: r['mtime'], reverse=True)
    for r in results:
        del r['mtime']
    return {'results': results[:n]}


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
    project = ''
    body = content
    if content.startswith('---'):
        end = content.find('---', 3)
        if end != -1:
            for line in content[3:end].splitlines():
                if line.startswith('title:'):
                    title = line.partition(':')[2].strip().strip('"\'')
                elif line.startswith('project:'):
                    project = line.partition(':')[2].strip().strip('"\'')
            body = content[end + 3:].lstrip('\n')
    return {'filename': filename, 'title': title, 'body': body, 'project': project}


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
            has_reviewed = has_updated = has_project = False
            for line in fm_lines:
                if line.startswith('reviewed:'):
                    new_fm.append('reviewed: unreviewed')
                    has_reviewed = True
                elif line.startswith('updated:'):
                    new_fm.append(f'updated: {today}')
                    has_updated = True
                elif line.startswith('project:') and req.project is not None:
                    if req.project:
                        new_fm.append(f'project: {req.project}')
                    # omit line if project is being cleared
                    has_project = True
                else:
                    new_fm.append(line)
            if not has_reviewed:
                new_fm.append('reviewed: unreviewed')
            if not has_updated:
                new_fm.append(f'updated: {today}')
            if req.project and not has_project:
                new_fm.append(f'project: {req.project}')
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


# ── API: Paperless ingest ─────────────────────────────────────────────────────

@api.post('/ingest/paperless')
def ingest_paperless(req: PaperlessIngestRequest):
    from core.search import get_store
    from langchain_core.documents import Document

    store = get_store()
    source = f'paperless:{req.paperless_id}'
    meta_parts = [
        f'Correspondent: {req.correspondent}' if req.correspondent else '',
        f'Type: {req.doc_type}' if req.doc_type else '',
        f'Tags: {", ".join(req.tags)}' if req.tags else '',
        f'Date: {req.created}' if req.created else '',
    ]
    headers = ' | '.join(p for p in meta_parts if p)

    chunk_size, overlap = 2000, 200
    text = req.content.strip()
    docs = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        docs.append(Document(
            page_content=text[start:end],
            metadata={'filename': req.title, 'folder': 'paperless', 'headers': headers},
        ))
        if end == len(text):
            break
        start = end - overlap

    store.upsert_file(source, docs)
    return {'ok': True, 'source': source, 'chunks_stored': len(docs)}


@api.delete('/ingest/paperless/{paperless_id}')
def delete_paperless(paperless_id: int):
    from core.search import get_store
    store = get_store()
    deleted = store.delete_file(f'paperless:{paperless_id}')
    return {'ok': True, 'chunks_deleted': deleted}


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
from features.review import (
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
    # Also search by filename slug to surface thematically related notes
    filename_query = ' '.join(
        n['filename'].replace('.md', '').replace('-', ' ').replace('_', ' ')
        for n in notes_data
    )
    rag_context = ''
    try:
        seen_sources: set[str] = set()
        best_chunks: list[dict] = []
        for query in [combined_text, filename_query]:
            _, _, chunks = search(query)
            for c in chunks:
                src = c.get('source', '')
                # Exclude the notes being reviewed from their own RAG context
                if src not in seen_sources and not any(n['filename'] in src for n in notes_data):
                    seen_sources.add(src)
                    best_chunks.append(c)
                if len(best_chunks) >= 5:
                    break
            if len(best_chunks) >= 5:
                break
        rag_context = '\n'.join(f"[{c['source']}] {c['content'][:200]}" for c in best_chunks[:5])
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



# ── API: kanban (native) ──────────────────────────────────────────────────────
# Hot-path todo CRUD and swimlanes are served natively from CT 111.
# Enrich, agent/turn, and settings remain proxied to CT 122 (:3000).

_SWIMLANES_FILE = Path('/mnt/Claude/context/swimlanes.json')
_DEFAULT_SWIMLANES = ['Dev', 'Infra', 'Personal']
_PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
_STATUS_MAP = {
    'inbox': 'inbox', 'pending': 'pending',
    'in-progress': 'in_progress', 'in_progress': 'in_progress',
    'completed': 'completed', 'wontdo': 'wontdo', 'partial': 'partial',
}


def _get_fm_field(content: str, patterns: list, default=None):
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"')
    return default


def _parse_todo_file(filename: str, filepath: Path) -> dict | None:
    try:
        content = filepath.read_text(encoding='utf-8', errors='replace')
        id_m = re.match(r'^(\d+)', filename)
        todo_id = int(id_m.group(1)) if id_m else None

        title = (_get_fm_field(content, [
            r'^title:\s*\"?([^\"\n]+)\"?\s*$',
            r'^#\s+(.+)',
        ]) or filename.replace('.md', '').lstrip('0123456789-'))

        _raw_status = _get_fm_field(content, [r'^status:\s*([a-zA-Z0-9_-]+)\s*$']) or 'pending'
        status = 'pending' if _raw_status == 'todo' else _raw_status
        priority   = _get_fm_field(content, [r'^priority:\s*(high|medium|low)\s*$']) or 'medium'
        created    = _get_fm_field(content, [r'^created:\s*([\d-]+)\s*$']) or ''
        completed  = _get_fm_field(content, [r'^completed:\s*([\d-]+)\s*$'])
        swimlane   = _get_fm_field(content, [r'^swimlane:\s*\"?([^\"\n]+)\"?\s*$'])
        assignee   = _get_fm_field(content, [r'^assignee:\s*([^\n]+)\s*$'])
        project    = _get_fm_field(content, [r'^project:\s*([^\n]+)\s*$'])
        complexity = _get_fm_field(content, [r'^complexity:\s*(small|medium|large)\s*$'])

        prereq_ids: list[str] = []
        ym = re.search(r'^(?:prereqs|prereqIds):\s*\[([^\]]*)\]\s*$', content,
                       re.IGNORECASE | re.MULTILINE)
        if ym:
            prereq_ids = [s.strip().strip('"\'') for s in ym.group(1).split(',') if s.strip()]

        return {
            'id': todo_id, 'title': title, 'status': status, 'priority': priority,
            'created': created, 'completed': completed, 'prereqIds': prereq_ids,
            'swimlane': swimlane, 'assignee': assignee, 'project': project,
            'complexity': complexity,
        }
    except Exception:
        return None


def _find_todo_path(todo_id: int) -> Path | None:
    for p in _todos_dir.glob('*.md'):
        m = re.match(r'^(\d+)', p.name)
        if m and int(m.group(1)) == todo_id:
            return p
    return None


def _set_fm_field(content: str, field: str, value: str) -> str:
    pattern = re.compile(rf'^({re.escape(field)}:\s*).*$', re.IGNORECASE | re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(f'{field}: {value}', content, count=1)
    return content.replace('---\n', f'---\n{field}: {value}\n', 1)


def _load_swimlanes() -> list[str]:
    try:
        if _SWIMLANES_FILE.exists():
            return json.loads(_SWIMLANES_FILE.read_text())
    except Exception:
        pass
    return list(_DEFAULT_SWIMLANES)


def _save_swimlanes(lanes: list[str]):
    _SWIMLANES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SWIMLANES_FILE.write_text(json.dumps(lanes, indent=2))


# ── kanban todo routes ────────────────────────────────────────────────────────

@api.get('/kanban/todos')
def kanban_todos_list(
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    swimlane: str | None = None,
    sort: str = 'priority',
):
    todos = []
    for p in sorted(_todos_dir.glob('*.md')):
        t = _parse_todo_file(p.name, p)
        if t:
            todos.append(t)

    if status:
        norm = _STATUS_MAP.get(status, status)
        todos = [t for t in todos if t['status'] == norm]
    if priority:
        todos = [t for t in todos if t['priority'] == priority]
    if assignee:
        todos = [t for t in todos if t['assignee'] == assignee]
    if swimlane:
        todos = [t for t in todos if t['swimlane'] == swimlane]

    if sort == 'priority':
        todos.sort(key=lambda t: (_PRIORITY_ORDER.get(t['priority'], 999), t['id'] or 0))
    elif sort == 'id':
        todos.sort(key=lambda t: t['id'] or 0)

    return todos


@api.post('/kanban/todos')
async def kanban_todos_create_native(request: Request):
    data = json.loads(await request.body())
    req = TodoCreateRequest(
        title=data.get('title', ''),
        description=data.get('description', ''),
        priority=data.get('priority', 'medium'),
        swimlane=data.get('swimlane', 'Dev'),
        assignee=data.get('assignee', ''),
        project=data.get('project', ''),
        complexity=data.get('complexity', ''),
    )
    return todos_create(req)


@api.get('/kanban/todos/{todo_id}')
def kanban_todos_get(todo_id: int):
    filepath = _find_todo_path(todo_id)
    if not filepath:
        raise HTTPException(status_code=404, detail=f'Todo not found: {todo_id}')
    return _parse_todo_file(filepath.name, filepath)


@api.patch('/kanban/todos/{todo_id}')
async def kanban_todos_patch(todo_id: int, request: Request):
    updates = json.loads(await request.body())

    if 'status' in updates and updates['status'] not in \
            {'inbox', 'pending', 'in_progress', 'completed', 'wontdo', 'partial'}:
        raise HTTPException(status_code=400, detail=f'Invalid status: {updates["status"]}')
    if 'priority' in updates and updates['priority'] not in {'high', 'medium', 'low'}:
        raise HTTPException(status_code=400, detail=f'Invalid priority: {updates["priority"]}')

    filepath = _find_todo_path(todo_id)
    if not filepath:
        raise HTTPException(status_code=404, detail=f'Todo not found: {todo_id}')

    content = filepath.read_text(encoding='utf-8')

    if 'status' in updates:
        content = _set_fm_field(content, 'status', updates['status'])
        if updates['status'] == 'completed':
            content = _set_fm_field(content, 'completed', date.today().isoformat())
    if 'priority' in updates:
        content = _set_fm_field(content, 'priority', updates['priority'])
    if 'swimlane' in updates:
        content = _set_fm_field(content, 'swimlane', str(updates.get('swimlane') or '').strip())
    if 'title' in updates:
        safe = str(updates['title']).strip().replace('"', '\\"')
        content = _set_fm_field(content, 'title', f'"{safe}"')
    if 'project' in updates:
        content = _set_fm_field(content, 'project', str(updates.get('project') or '').strip())
    if 'assignee' in updates:
        content = _set_fm_field(content, 'assignee', str(updates.get('assignee') or '').strip())
    if 'complexity' in updates:
        val = str(updates.get('complexity') or '').strip()
        if val in {'small', 'medium', 'large'}:
            content = _set_fm_field(content, 'complexity', val)
        elif val == '':
            content = re.sub(r'^complexity:[^\n]*\n?', '', content, flags=re.MULTILINE)
    if 'body' in updates and isinstance(updates['body'], str):
        fm_end = content.find('---', 3)
        if fm_end != -1:
            content = content[:fm_end + 3] + '\n\n' + updates['body'].strip() + '\n'
        else:
            content = updates['body'].strip() + '\n'

    filepath.write_text(content, encoding='utf-8')
    return _parse_todo_file(filepath.name, filepath)


@api.get('/kanban/todos/{todo_id}/raw')
def kanban_todos_raw(todo_id: int):
    filepath = _find_todo_path(todo_id)
    if not filepath:
        raise HTTPException(status_code=404, detail=f'Todo not found: {todo_id}')
    content = filepath.read_text(encoding='utf-8')
    body = re.sub(r'^---[\s\S]*?---\n?', '', content, count=1).strip()
    return {'body': body}


@api.get('/kanban/refresh')
def kanban_refresh():
    count = sum(1 for _ in _todos_dir.glob('*.md'))
    return {'ok': True, 'count': count}


# ── kanban swimlane routes ────────────────────────────────────────────────────

@api.get('/kanban/swimlanes')
def kanban_swimlanes_list():
    return _load_swimlanes()


@api.post('/kanban/swimlanes')
async def kanban_swimlanes_add(request: Request):
    data = json.loads(await request.body())
    name = str(data.get('name', '')).strip()
    if not name:
        raise HTTPException(status_code=400, detail='name is required')
    lanes = _load_swimlanes()
    normalized = name.title()
    if normalized not in lanes:
        lanes.append(normalized)
        _save_swimlanes(lanes)
    return lanes


@api.delete('/kanban/swimlanes/{name}')
def kanban_swimlanes_delete(name: str):
    lanes = _load_swimlanes()
    lanes = [l for l in lanes if l.lower() != name.lower()]
    _save_swimlanes(lanes)
    return lanes


# ── kanban: agent/turn proxy (CT 122) ────────────────────────────────────────

def _kanban_proxy(method: str, path: str, body: bytes = b'', qs: str = ''):
    url = f'{_TODOS_INDEXER}{path}'
    if qs:
        url += f'?{qs}'
    hdrs = {'Content-Type': 'application/json'} if body else {}
    try:
        r = getattr(http_requests, method)(url, data=body or None, headers=hdrs, timeout=30)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get('Content-Type', 'application/json'))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'Kanban proxy unavailable: {e}')


@api.post('/kanban/agent/turn')
async def kanban_agent_turn(request: Request):
    return _kanban_proxy('post', '/agent/turn', body=await request.body())


_models_config_path = Path('/mnt/Claude/config/models.json')
_model_health_path  = Path('/mnt/Claude/config/model-health.json')


# ── OpenRouter helper ─────────────────────────────────────────────────────────

def _call_openrouter(prompt: str) -> str:
    """POST a single user prompt to OpenRouter. Returns the response text."""
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if not key:
        raise ValueError('OPENROUTER_API_KEY not set')
    model = 'google/gemini-2.0-flash-lite:free'
    try:
        if _models_config_path.exists():
            cfg = json.loads(_models_config_path.read_text())
            raw = cfg.get('notes-rag', model)
            model = re.sub(r'^openrouter/', '', raw)
    except Exception:
        pass
    r = http_requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
        json={'model': model, 'messages': [{'role': 'user', 'content': prompt}]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


# ── kanban: auto prereq detection ─────────────────────────────────────────────

@api.post('/kanban/todos/{todo_id}/prereqs')
def kanban_todos_find_prereqs(todo_id: int):
    filepath = _find_todo_path(todo_id)
    if not filepath:
        raise HTTPException(status_code=404, detail=f'Todo not found: {todo_id}')

    todo = _parse_todo_file(filepath.name, filepath)
    if not todo:
        raise HTTPException(status_code=500, detail='Failed to parse todo')

    content = filepath.read_text(encoding='utf-8')
    body = re.sub(r'^---[\s\S]*?---\n?', '', content, count=1).strip()

    # Collect non-completed todos in the same swimlane or project
    scope_key = todo.get('swimlane') or todo.get('project')
    if not scope_key:
        return {'prereqIds': [], 'reason': 'no-scope'}

    candidates: list[dict] = []
    for p in _todos_dir.glob('*.md'):
        t = _parse_todo_file(p.name, p)
        if not t or t['id'] == todo_id:
            continue
        if t['status'] in ('completed', 'wontdo'):
            continue
        if t.get('swimlane') == scope_key or t.get('project') == scope_key:
            candidates.append(t)

    if not candidates:
        return {'prereqIds': []}

    # RAG search for workspace context
    rag_context = ''
    try:
        query = f"{todo['title']} — {body[:300]}" if body else todo['title']
        answer, _, _ = search(query)
        if answer and len(answer) > 30:
            rag_context = answer
    except Exception:
        pass

    # Build LLM prompt
    scope_label = scope_key or 'same scope'
    todo_list = '\n'.join(f"- #{t['id']}: {t['title']}" for t in candidates)
    prompt = (
        'You are analyzing a task management system. '
        'Given the following todo item, identify which other todos from the list '
        'MUST be completed BEFORE this one can start.\n\n'
        f'Current todo:\nTitle: {todo["title"]}\nBody: {body or "(no body)"}\n\n'
        + (f'Workspace context:\n{rag_context}\n\n' if rag_context else '')
        + f'Other todos in {scope_label}:\n{todo_list}\n\n'
        'Return ONLY a JSON array of integer IDs that are prerequisites, e.g. [7, 8]. '
        'If none apply, return []. No explanation.'
    )

    try:
        response = _call_openrouter(prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'LLM call failed: {e}')

    # Parse and validate the returned IDs
    m = re.search(r'\[([^\]]*)\]', response)
    prereq_ids: list[int] = []
    if m:
        valid_ids = {t['id'] for t in candidates}
        for token in m.group(1).split(','):
            token = token.strip()
            if re.match(r'^\d+$', token) and int(token) in valid_ids:
                prereq_ids.append(int(token))

    # Write prereqIds to frontmatter (only if non-empty)
    if prereq_ids:
        id_str = ', '.join(str(i) for i in prereq_ids)
        content = _set_fm_field(content, 'prereqIds', f'[{id_str}]')
        filepath.write_text(content, encoding='utf-8')

    return {'prereqIds': prereq_ids}



# ── API: services health ──────────────────────────────────────────────────────

_SERVICES = [
    {'id': 'notes',    'label': 'Notes RAG',  'url': 'http://192.168.88.71:8080/api/health'},
    {'id': 'kanban',   'label': 'Kanban',      'url': 'http://192.168.88.78:3000'},
    {'id': 'reports',  'label': 'Reports',     'url': 'http://localhost:8082/reports'},
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




# ── API: Hermes queue status ───────────────────────────────────────────────────
_HERMES_STATUS_FILE = '/mnt/Claude/config/hermes-status.json'

@api.get('/hermes/status')
def hermes_status():
    try:
        with open(_HERMES_STATUS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {
        'research': {'status': 'idle', 'topic': None, 'started_at': None, 'finished_at': None},
        'forecast': {'status': 'idle', 'topic': None, 'started_at': None, 'finished_at': None},
    }


# ── API: LLM models ────────────────────────────────────────────────────────────

_SIDECAR_URLS = {
    'hermes':   'http://192.168.88.83:8090/reload',
    'mirofish': 'http://192.168.88.79:8091/reload',
    'openclaw': 'http://192.168.88.63:8092/reload',
}
_DISCORD_SERVICES = frozenset({'hermes', 'openclaw'})
_ALL_SERVICES     = ['notes-rag', 'hermes', 'mirofish', 'openclaw', 'paperless-gpt']
_SERVICE_LABELS   = {
    'notes-rag':     'Notes RAG',
    'hermes':        'Hermes',
    'mirofish':      'MiroFish',
    'openclaw':      'OpenClaw',
    'paperless-gpt': 'Paperless OCR',
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
