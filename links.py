"""Link scan — supersession + related-note detection.

User-triggered scan over a note N against the corpus. Returns two
categories of candidates:
  - supersessions: N is the current/correct version, A is outdated
  - related: A and N cover the same topic, neither replaces the other

Commit writes frontmatter on both sides; rejections are persisted to
`rejected_link_candidates` on the source note so the pair is not re-suggested.

See docs/specs/2026-04-21-link-scan-design.md.
"""

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Callable

import yaml

from review import parse_frontmatter


# ── Frontmatter I/O ─────────────────────────────────────────────────────────

def load_note(path: Path) -> tuple[dict, str]:
    content = path.read_text(encoding='utf-8', errors='replace')
    return parse_frontmatter(content)


def serialise_note(fm: dict, body: str) -> str:
    if not fm:
        return body
    lines = ['---']
    for k, v in fm.items():
        dumped = yaml.dump({k: v}, default_flow_style=False, sort_keys=False).strip()
        lines.append(dumped)
    lines.append('---')
    body_clean = body.lstrip('\n')
    joined = '\n'.join(lines) + '\n\n' + body_clean
    if not joined.endswith('\n'):
        joined += '\n'
    return joined


def _tmp_path(path: Path) -> Path:
    return path.parent / f'.tmp_{os.getpid()}_{path.name}'


def atomic_write_pair(path_a: Path, content_a: str, path_b: Path, content_b: str) -> None:
    """Write both files, rolling back A if B fails. Both must already exist."""
    tmp_a, tmp_b = _tmp_path(path_a), _tmp_path(path_b)
    backup_a = path_a.read_text(encoding='utf-8', errors='replace')
    try:
        tmp_a.write_text(content_a, encoding='utf-8')
        tmp_b.write_text(content_b, encoding='utf-8')
    except Exception:
        tmp_a.unlink(missing_ok=True)
        tmp_b.unlink(missing_ok=True)
        raise
    os.replace(str(tmp_a), str(path_a))
    try:
        os.replace(str(tmp_b), str(path_b))
    except Exception:
        path_a.write_text(backup_a, encoding='utf-8')
        tmp_b.unlink(missing_ok=True)
        raise


def atomic_write_single(path: Path, content: str) -> None:
    tmp = _tmp_path(path)
    try:
        tmp.write_text(content, encoding='utf-8')
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── Exclusion set ───────────────────────────────────────────────────────────

def build_exclusion_set(fm: dict, self_filename: str) -> set[str]:
    excluded = {self_filename}
    for field in ('supersedes', 'related', 'rejected_link_candidates'):
        v = fm.get(field)
        if isinstance(v, list):
            excluded.update(str(x) for x in v)
    sb = fm.get('superseded_by')
    if sb:
        excluded.add(str(sb))
    return excluded


# ── LLM judge ───────────────────────────────────────────────────────────────

_JUDGE_PROMPT = """You are classifying the relationship between two notes from a personal knowledge base. Respond in strict JSON.

Note A (existing):
{a}

Note B (new):
{b}

Does B supersede A (i.e. B is the current, correct version and A is now outdated)? Or do A and B cover the same topic without one replacing the other? Or neither?

Return: {{"relation": "supersedes" | "related" | "none", "reason": "<one sentence>"}}"""


def judge_relation(note_a_body: str, note_b_body: str, llm) -> tuple[str, str]:
    prompt = _JUDGE_PROMPT.format(a=note_a_body[:2000], b=note_b_body[:2000])
    try:
        resp = llm.invoke(prompt)
        txt = resp.content if hasattr(resp, 'content') else str(resp)
    except Exception as e:
        return ('none', f'LLM error: {e}')
    m = re.search(r'\{.*\}', txt, re.DOTALL)
    if not m:
        return ('none', 'LLM returned no JSON')
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ('none', 'Invalid JSON from LLM')
    rel = data.get('relation', 'none')
    if rel not in ('supersedes', 'related', 'none'):
        return ('none', f'unexpected relation: {rel}')
    return (rel, str(data.get('reason', '')))


# ── Scan orchestrator ───────────────────────────────────────────────────────

def scan_links(
    note_path: Path,
    resolve_path: Callable[[str], Path | None],
    retrieve_fn: Callable,
    llm,
    k: int = 10,
    threshold: float = 0.6,
) -> dict:
    """Run the detection pipeline for note at `note_path`.

    `retrieve_fn(query, k)` returns an iterable of Document-like objects; each
    must expose `metadata.get('filename')` and optionally `metadata.get('similarity')`.
    """
    fm, body = load_note(note_path)
    self_filename = note_path.name
    exclusion = build_exclusion_set(fm, self_filename)

    docs = retrieve_fn(body, k=k)
    seen: set[str] = set()
    candidates: list[tuple[str, float]] = []
    for doc in docs:
        fname = doc.metadata.get('filename', '')
        if not fname or fname in exclusion or fname in seen:
            continue
        sim = float(doc.metadata.get('similarity', 0.0))
        if sim < threshold:
            continue
        seen.add(fname)
        candidates.append((fname, sim))

    results: dict = {'supersessions': [], 'related': []}
    for fname, sim in candidates:
        c_path = resolve_path(fname)
        if c_path is None or not c_path.exists():
            continue
        try:
            c_fm, c_body = load_note(c_path)
        except Exception:
            continue
        if c_fm.get('superseded_by'):
            continue
        rel, reason = judge_relation(c_body, body, llm)
        if rel == 'supersedes':
            results['supersessions'].append({'path': fname, 'reason': reason, 'similarity': round(sim, 4)})
        elif rel == 'related':
            results['related'].append({'path': fname, 'reason': reason, 'similarity': round(sim, 4)})
    return results


# ── Commit: supersedes ──────────────────────────────────────────────────────

class ConflictError(Exception):
    """Raised when target is already superseded."""


def commit_supersedes(source_path: Path, target_path: Path) -> tuple[dict, dict]:
    """Mark target as superseded by source. Returns (new source fm, new target fm).

    Raises ConflictError if target.superseded_by is already set.
    """
    source_fm, source_body = load_note(source_path)
    target_fm, target_body = load_note(target_path)

    if target_fm.get('superseded_by'):
        raise ConflictError(f'{target_path.name} is already superseded by {target_fm["superseded_by"]}')

    target_fm['superseded_by'] = source_path.name
    target_fm['superseded_at'] = date.today().isoformat()

    supersedes = source_fm.get('supersedes')
    if not isinstance(supersedes, list):
        supersedes = []
    if target_path.name not in supersedes:
        supersedes.append(target_path.name)
    source_fm['supersedes'] = supersedes

    new_source = serialise_note(source_fm, source_body)
    new_target = serialise_note(target_fm, target_body)
    atomic_write_pair(source_path, new_source, target_path, new_target)
    return (source_fm, target_fm)


# ── Commit: related ─────────────────────────────────────────────────────────

def _append_unique(fm: dict, field: str, value: str) -> bool:
    lst = fm.get(field)
    if not isinstance(lst, list):
        lst = []
    if value in lst:
        return False
    lst.append(value)
    fm[field] = lst
    return True


def commit_related(source_path: Path, target_path: Path) -> tuple[dict, dict]:
    """Add symmetric `related` links between source and target. Idempotent."""
    source_fm, source_body = load_note(source_path)
    target_fm, target_body = load_note(target_path)

    changed_source = _append_unique(source_fm, 'related', target_path.name)
    changed_target = _append_unique(target_fm, 'related', source_path.name)

    if changed_source or changed_target:
        new_source = serialise_note(source_fm, source_body)
        new_target = serialise_note(target_fm, target_body)
        atomic_write_pair(source_path, new_source, target_path, new_target)
    return (source_fm, target_fm)


# ── Reject ──────────────────────────────────────────────────────────────────

def reject_candidate(source_path: Path, target_filename: str) -> dict:
    fm, body = load_note(source_path)
    _append_unique(fm, 'rejected_link_candidates', target_filename)
    atomic_write_single(source_path, serialise_note(fm, body))
    return fm
