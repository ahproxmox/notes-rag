"""Notes review engine — scanning, sessions, interviews, frontmatter."""

import json
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

def _get_review_model_name() -> str:
    try:
        cfg = json.loads(open('/mnt/Claude/config/models.json').read())
        return cfg.get('notes-rag') or os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite')
    except Exception:
        return os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite')


def _get_review_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
        api_key=os.environ.get('OPENROUTER_API_KEY', ''),
        model=_get_review_model_name(),
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


def _detect_note_intent(filename: str, body: str) -> str:
    """Classify a note's intent to drive smarter question selection.

    Returns one of: decision, idea, task, reference, journal, list
    """
    fname = filename.lower().replace('-', ' ').replace('_', ' ')
    text = (fname + ' ' + body[:400]).lower()

    decision_signals = ['decided', 'decision', 'chose', 'going with', 'will use', 'switching to',
                        'vs ', ' or ', 'trade-off', 'tradeoff', 'pros', 'cons', 'alternative']
    task_signals = ['todo', 'task', 'action', 'need to', 'should ', 'must ', 'deploy', 'install',
                    'set up', 'fix ', 'update ', 'migrate', 'implement', 'create', 'build']
    idea_signals = ['idea', 'concept', 'thinking about', 'what if', 'could ', 'might ', 'explore',
                    'consider', 'brainstorm', 'proposal', 'draft', 'experiment']
    reference_signals = ['docs', 'documentation', 'reference', 'config', 'credentials', 'setup',
                         'how to', 'howto', 'guide', 'cheatsheet', 'notes on', 'overview']
    journal_signals = ['today', 'yesterday', 'this week', 'meeting', 'call with', 'talked',
                       'discussed', 'reflection', 'feeling', 'learned', 'realised', 'realized']

    scores = {
        'decision': sum(1 for s in decision_signals if s in text),
        'task': sum(1 for s in task_signals if s in text),
        'idea': sum(1 for s in idea_signals if s in text),
        'reference': sum(1 for s in reference_signals if s in text),
        'journal': sum(1 for s in journal_signals if s in text),
    }

    lines_ = [l for l in body.strip().split('\n') if l.strip() and not l.strip().startswith('#')]
    list_lines = [l for l in lines_ if re.match(r'^\s*[-*\d]+[.)]\s', l)]
    if list_lines and len(list_lines) >= len(lines_) * 0.6 and max(scores.values(), default=0) < 2:
        return 'list'

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 1 else 'reference'


# ---------------------------------------------------------------------------
# Interview engine
# ---------------------------------------------------------------------------

_INTENT_QUESTION_GUIDE = {
    'decision': (
        "Focus on: what alternatives were considered and ruled out, what would change this decision, "
        "whether it's reversible, and what the first concrete step is."
    ),
    'idea': (
        "Focus on: what triggered this idea, what problem it actually solves, the smallest version "
        "you could test, and any dependencies or blockers."
    ),
    'task': (
        "Focus on: what success looks like, any blockers or dependencies, priority relative to other "
        "work, and whether anything could go wrong."
    ),
    'reference': (
        "Focus on: why this was worth capturing, what scenario you'd return to it in, and whether "
        "anything is missing that would make it more useful."
    ),
    'journal': (
        "Focus on: what action or follow-up this implies, whether it changes anything, and what "
        "context future-Angelo would need to understand why this mattered."
    ),
    'list': (
        "Focus on: what the list is for and what drives inclusion, what's notably missing, and "
        "whether items are ranked or prioritised in any way."
    ),
}


def build_interview_prompt(
    notes: list,
    rag_context: str,
    previous_reviews: list,
    question_count: int,
) -> str:
    grouped = len(notes) > 1

    # Detect intent for each note and build annotated sections
    note_sections = []
    intents = []
    for n in notes:
        intent = _detect_note_intent(n['filename'], n['body'])
        intents.append(intent)
        style = _detect_note_style(n['body'])
        style_hint = ''
        if style == 'sparse':
            style_hint = ' [SPARSE — expand with open-ended questions]'
        note_sections.append(f"### {n['filename']} [{intent.upper()}]{style_hint}\n{n['body']}")
    notes_text = '\n\n'.join(note_sections)

    # Dominant intent drives question strategy (use first if grouped)
    dominant_intent = intents[0] if intents else 'reference'
    intent_guide = _INTENT_QUESTION_GUIDE.get(dominant_intent, _INTENT_QUESTION_GUIDE['reference'])

    # Question progression guidance based on position
    progression = {
        0: "Q1: Understand the core why — motivation, trigger, or context behind this note.",
        1: "Q2: Explore implications, connections to other work, or what comes next.",
        2: "Q3: Surface what's missing — gaps, risks, or what future-Angelo would want to know.",
    }.get(question_count, "Ask a clarifying question about what's most unclear.")

    prev_section = ''
    if previous_reviews:
        prev_lines = '\n'.join(f"- {qa['q']}" for qa in previous_reviews)
        prev_section = f'\nDO NOT REPEAT these already-asked questions:\n{prev_lines}\n'

    rag_section = ''
    if rag_context:
        rag_section = (
            f'\nRELATED EXISTING NOTES:\n{rag_context}\n'
            'If relevant, ask how this note relates to, extends, or supersedes those notes. '
            'Name specific related notes in your question if it adds value.\n'
        )

    grouped_note = ''
    if grouped:
        grouped_note = (
            f'\nReviewing {len(notes)} related notes together. '
            'Ask a question that bridges across all of them, not just one.\n'
        )

    return (
        "You are a concise notes interviewer enriching Angelo's personal knowledge base.\n"
        f"Ask ONE question only. No preamble, no explanation — just the question itself.{grouped_note}\n"
        f"NOTE TYPE: {dominant_intent} — {intent_guide}\n"
        f"QUESTION STRATEGY: {progression}\n"
        "RULES:\n"
        "- NEVER answer, explain, or research the topic\n"
        "- Keep the question specific to what's actually in the note\n"
        f"- This is question {question_count + 1} of max 3\n"
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
