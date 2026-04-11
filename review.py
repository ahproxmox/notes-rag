"""Notes review engine — scanning, sessions, interviews, frontmatter."""

import os
import re
import yaml
from datetime import date, datetime
from typing import Any


def _coerce_dates(fm: dict) -> dict:
    """Convert date/datetime values in frontmatter to ISO strings."""
    out = {}
    for k, v in fm.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown. Returns (frontmatter_dict, body)."""
    if not content.startswith('---'):
        return {}, content

    m = re.search(r'^\-\-\-\s*$', content[3:], re.MULTILINE)
    end = (3 + m.start()) if m else -1
    if end == -1:
        return {}, content

    fm_str = content[3:end].strip()
    body = content[end + 3:].lstrip('\n')

    try:
        fm = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError:
        return {}, content

    fm = _coerce_dates(fm)
    return fm, body


def write_frontmatter(
    fm: dict,
    body: str,
    tags: list[str],
    review_num: int,
    review_content: str,
) -> str:
    """Rebuild a note with updated frontmatter and appended review section."""
    lines = ['---']
    if 'date_created' in fm:
        lines.append(f"date_created: {fm['date_created']}")
    lines.append('reviewed: true')
    tag_str = ', '.join(tags)
    lines.append(f"tags: [{tag_str}]")
    lines.append(f"review_count: {review_num}")
    skip = {'date_created', 'reviewed', 'tags', 'review_count'}
    for k, v in fm.items():
        if k not in skip:
            dumped = yaml.dump({k: v}, default_flow_style=False).strip()
            lines.append(dumped)
    lines.append('---')

    review_section = f"\n## Review {review_num}\n{review_content}\n"
    new_body = body.rstrip('\n') + '\n' + review_section

    return '\n'.join(lines) + '\n' + new_body


def scan_unreviewed(notes_dir: str) -> list[dict]:
    """Scan directory for .md files with reviewed: unreviewed frontmatter."""
    results = []
    if not os.path.isdir(notes_dir):
        return results

    for fname in os.listdir(notes_dir):
        if not fname.endswith('.md'):
            continue
        path = os.path.join(notes_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue

        fm, body = parse_frontmatter(content)
        if fm.get('reviewed') != 'unreviewed':
            continue

        body_stripped = body.strip()
        body_lines_content = [
            l for l in body_stripped.split('\n')
            if l.strip() and not l.strip().startswith('#')
        ]
        if not body_lines_content:
            continue

        preview_lines = body_lines_content[:2]
        preview = ' '.join(l.strip() for l in preview_lines)
        if len(preview) > 120:
            preview = preview[:117] + '...'

        results.append({
            'filename': fname,
            'path': path,
            'date_created': str(fm.get('date_created', '')),
            'preview': preview,
            'body_line_count': len(body_lines_content),
            'review_count': fm.get('review_count', 0),
            'tags': fm.get('tags', []) or [],
        })

    results.sort(key=lambda r: r['date_created'])
    return results

import uuid


# ---------------------------------------------------------------------------
# Note grouping
# ---------------------------------------------------------------------------

def group_notes(
    notes: list[dict],
    similarity: dict[tuple[str, str], float],
    threshold: float = 0.4,
) -> list[dict]:
    """Group notes by pairwise similarity using union-find.

    Args:
        notes: list of note dicts with filename key
        similarity: dict mapping (filename_a, filename_b) -> float score
        threshold: minimum score to group together

    Returns list of group dicts: {group_id, filenames, label}
    """
    filenames = [n["filename"] for n in notes]
    parent = {f: f for f in filenames}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (a, b), score in similarity.items():
        if score >= threshold and a in parent and b in parent:
            union(a, b)

    groups_map: dict[str, list[str]] = {}
    for f in filenames:
        root = find(f)
        groups_map.setdefault(root, []).append(f)

    groups = []
    for i, (root, members) in enumerate(groups_map.items()):
        groups.append({
            'group_id': f'g{i}',
            'filenames': members,
            'label': members[0].replace('.md', '').replace('-', ' '),
        })
    return groups


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

class SessionManager:
    """In-memory session store for active note reviews."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def create(self, filenames: list[str], notes_data: list[dict]) -> dict:
        """Create a new review session."""
        session_id = uuid.uuid4().hex[:12]
        session = {
            'session_id': session_id,
            'notes': notes_data,
            'qa': [],
            'question_count': 0,
            'pending_question': '',
            'done': False,
        }
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> dict | None:
        return self._sessions.get(session_id)

    def add_qa(self, session_id: str, question: str, answer: str):
        session = self._sessions.get(session_id)
        if session:
            session['qa'].append({'q': question, 'a': answer})
            session['question_count'] += 1

    def mark_done(self, session_id: str):
        session = self._sessions.get(session_id)
        if session:
            session['done'] = True

    def remove(self, session_id: str):
        self._sessions.pop(session_id, None)

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from typing import AsyncGenerator


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _get_review_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
        api_key=os.environ.get('OPENROUTER_API_KEY', ''),
        model=os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite'),
        temperature=0.7,
        max_tokens=200,
    )


def _detect_note_style(body: str) -> str:
    lines_ = [l for l in body.strip().split(chr(10)) if l.strip() and not l.strip().startswith('#')]
    if len(lines_) <= 2:
        return 'sparse'
    list_lines = [l for l in lines_ if re.match(r'^\s*[-*\d]+[.)]\s', l)]
    if list_lines and len(list_lines) >= len(lines_) * 0.6:
        return 'list'
    return 'normal'


# ---------------------------------------------------------------------------
# Interview engine
# ---------------------------------------------------------------------------

def build_interview_prompt(
    notes: list,
    rag_context: str,
    previous_reviews: list,
    question_count: int,
) -> str:
    grouped = len(notes) > 1
    note_sections = []
    for n in notes:
        style = _detect_note_style(n['body'])
        style_hint = ''
        if style == 'sparse':
            style_hint = ' [SHORT NOTE ask open-ended questions to expand: purpose, timeline, context]'
        elif style == 'list':
            style_hint = ' [LIST NOTE ask about the list as a whole: purpose, selection criteria, whats missing]'
        note_sections.append(f"### {n['filename']}{style_hint}\n{n['body']}")
    notes_text = '\n\n'.join(note_sections)
    prev_section = ''
    if previous_reviews:
        prev_lines = '\n'.join(f"- {qa['q']}" for qa in previous_reviews)
        prev_section = f'\nPREVIOUS REVIEW QUESTIONS do not repeat these:\n{prev_lines}\n'
    rag_section = ''
    if rag_context:
        rag_section = f'\nRELATED NOTES FROM KNOWLEDGE BASE:\n{rag_context}\nUse these to ask bridging questions.\n'
    grouped_note = ''
    if grouped:
        grouped_note = f'\nYou are reviewing {len(notes)} related notes together. Ask bridging questions that connect them.\n'
    return (
        "You are a notes interviewer helping Angelo enrich his personal notes.\n"
        f"Ask ONE contextual question at a time. No preamble, no explanation just the question.{grouped_note}\n"
        "RULES:\n"
        "- Ask about purpose, timeline, constraints, connections, next steps\n"
        "- NEVER answer, research, or explain the note topic\n"
        f"- Maximum 3 questions total (this is question {question_count + 1})\n"
        "- Stop earlier if the conversation reaches a natural conclusion\n"
        f"{prev_section}{rag_section}\n"
        "NOTES:\n"
        f"{notes_text}"
    )


async def generate_question(
    notes: list,
    rag_context: str,
    previous_reviews: list,
    qa_so_far: list,
    question_count: int,
):
    system = build_interview_prompt(notes, rag_context, previous_reviews, question_count)
    messages = [SystemMessage(content=system)]
    for qa in qa_so_far:
        messages.append(AIMessage(content=qa['q']))
        messages.append(HumanMessage(content=qa['a']))
    messages.append(HumanMessage(content='Ask your next question.'))
    llm = _get_review_llm()
    async for chunk in llm.astream(messages):
        if chunk.content:
            yield chunk.content


async def infer_tags(
    notes: list,
    rag_context: str,
    qa: list,
) -> list:
    note_sections = '\n\n'.join(f"### {n['filename']}\n{n['body']}" for n in notes)
    qa_section = '\n'.join(f"Q: {q['q']} A: {q['a']}" for q in qa) if qa else 'No interview.'
    prompt = (
        "Suggest 2-5 tags for these notes. Return ONLY a comma-separated list of lowercase hyphenated tags.\n\n"
        f"NOTES:\n{note_sections}\n\n"
        f"INTERVIEW:\n{qa_section}\n\n"
        f"RELATED:\n{rag_context}\n\n"
        "Tags:"
    )
    llm = _get_review_llm()
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    raw = response.content.strip()
    tags = [t.strip().lower().replace(' ', '-') for t in raw.split(',')]
    return [t for t in tags if t and re.match(r'^[a-z0-9-]+$', t)][:5]


def build_review_content(qa: list) -> str:
    today = date.today().isoformat()
    lines = [f'_{today}_']
    for pair in qa:
        lines.append(f"\n- **{pair['q']}** {pair['a']}")
    return '\n'.join(lines)
