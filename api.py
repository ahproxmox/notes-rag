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
@app.get('/manifest.json')
def manifest():
    return FileResponse(os.path.join(_ui_dir, 'manifest.json'), media_type='application/manifest+json')

@app.get('/sw.js')
def service_worker():
    return FileResponse(os.path.join(_ui_dir, 'sw.js'), media_type='application/javascript')




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

import json
from review import (
    scan_unreviewed, parse_frontmatter, write_frontmatter,
    group_notes, SessionManager, generate_question, infer_tags,
    build_review_content,
)

_session_mgr = SessionManager()
_notes_dir = '/mnt/Obsidian/Notes'


class ReviewStartRequest(BaseModel):
    note_ids: list[str]  # filenames e.g. ["Wedding notes.md"]


class ReviewReplyRequest(BaseModel):
    answer: str
    question: str = ''  # question this answer is for


# ---------------------------------------------------------------------------
# Review routes
# ---------------------------------------------------------------------------

@app.get('/review')
def review_page():
    return FileResponse(os.path.join(_ui_dir, 'review.html'))


@app.get('/review/queue')
def review_queue():
    """Scan for unreviewed notes, group by similarity, return triage queue."""
    notes = scan_unreviewed(_notes_dir)
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

@app.post('/review/start')
async def review_start(req: ReviewStartRequest):
    """Start a review session -- validates notes, loads content, gets RAG context, streams first question."""
    notes_data = []
    for fname in req.note_ids:
        path = os.path.join(_notes_dir, fname)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f'Note not found: {fname}')
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        fm, body = parse_frontmatter(content)

        # Extract Q&A from previous ## Review N sections for re-reviews
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

    # RAG context for all notes combined
    combined_text = ' '.join(n['body'][:200] for n in notes_data)
    rag_context = ''
    try:
        _, _, chunks = search(combined_text)
        rag_context = '\n'.join(
            f"[{c['source']}] {c['content'][:200]}" for c in chunks[:3]
        )
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

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.post('/review/{session_id}/reply')
async def review_reply(session_id: str, req: ReviewReplyRequest):
    """Process an interview answer and stream the next question (or signal done)."""
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


@app.post('/review/{session_id}/complete')
async def review_complete(session_id: str):
    """Complete interview -- infer tags, append review section, update frontmatter, verify write."""
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

        # Verify write
        try:
            with open(path, 'r', encoding='utf-8') as f:
                verify_fm, _ = parse_frontmatter(f.read())
            if verify_fm.get('reviewed') is not True:
                # Retry once
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


@app.post('/review/{note_id:path}/skip')
def review_skip(note_id: str):
    return {'skipped': note_id}


@app.post('/review/{note_id:path}/auto-tag')
async def review_auto_tag(note_id: str):
    """Auto-tag without interview -- infer tags from content and RAG context."""
    path = os.path.join(_notes_dir, note_id)
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
