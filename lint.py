#!/usr/bin/env python3
"""
Workspace lint / health-check script.
Writes a report to /mnt/Claude/inbox/lint-report.md

Checks:
  1. Review notes with empty related: [] field
  2. Stale context notes (no update in >90 days)
  3. Duplicate todo IDs
  4. Orphan wiki pages (wiki/ only — standalone dirs like memory/, inbox/ are exempt by design)
"""

import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

WORKSPACE = Path('/mnt/Claude')
OBSIDIAN  = Path('/mnt/Obsidian')
REVIEWS   = OBSIDIAN / 'Inbox' / 'Reviews'
CONTEXT   = WORKSPACE / 'context'
TODOS     = WORKSPACE / 'todos'
WIKI      = WORKSPACE / 'wiki'
REPORT    = WORKSPACE / 'inbox' / 'lint-report.md'

STALE_DAYS = 90
TODAY = date.today()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(path: Path) -> dict:
    """Return a dict of frontmatter fields, or {} if none."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return {}
    if not text.startswith('---'):
        return {}
    end = text.find('---', 3)
    if end == -1:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            fields[k.strip()] = v.strip()
    return fields


def parse_date(value: str) -> date | None:
    """Try to parse YYYY-MM-DD."""
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def all_md_files(root: Path, exclude: set[str] | None = None) -> list[Path]:
    exclude = exclude or set()
    results = []
    for p in root.rglob('*.md'):
        if any(part in exclude for part in p.parts):
            continue
        results.append(p)
    return results


# ---------------------------------------------------------------------------
# Check 1: Review notes with empty related:
# ---------------------------------------------------------------------------

def check_empty_related() -> list[str]:
    issues = []
    if not REVIEWS.exists():
        return issues
    for p in sorted(REVIEWS.glob('*.md')):
        fm = parse_frontmatter(p)
        related = fm.get('related', '').strip()
        if related in ('[]', '', '- []', 'null'):
            issues.append(str(p.relative_to(OBSIDIAN)))
    return issues


# ---------------------------------------------------------------------------
# Check 2: Stale context notes
# ---------------------------------------------------------------------------

def check_stale_context() -> list[tuple[str, str]]:
    """Return list of (path, date_str) for context notes not updated in >90 days."""
    issues = []
    if not CONTEXT.exists():
        return issues
    cutoff = TODAY - timedelta(days=STALE_DAYS)
    for p in sorted(CONTEXT.glob('*.md')):
        fm = parse_frontmatter(p)
        date_val = None
        for field in ('updated', 'date', 'created', 'date_created'):
            if field in fm:
                date_val = parse_date(fm[field])
                if date_val:
                    break
        if date_val is None:
            mtime = date.fromtimestamp(p.stat().st_mtime)
            date_val = mtime
        if date_val < cutoff:
            issues.append((str(p.relative_to(WORKSPACE)), date_val.isoformat()))
    return issues


# ---------------------------------------------------------------------------
# Check 3: Duplicate todo IDs
# ---------------------------------------------------------------------------

def check_duplicate_todo_ids() -> list[tuple[str, list[str]]]:
    """Return list of (id, [file1, file2, ...]) for colliding IDs."""
    if not TODOS.exists():
        return []
    id_to_files: dict[str, list[str]] = defaultdict(list)
    for p in sorted(TODOS.glob('*.md')):
        fm = parse_frontmatter(p)
        todo_id = fm.get('id', '').strip()
        if todo_id:
            id_to_files[todo_id].append(p.name)
    return [(tid, files) for tid, files in sorted(id_to_files.items()) if len(files) > 1]


# ---------------------------------------------------------------------------
# Check 4: Orphan wiki pages
# ---------------------------------------------------------------------------

def check_orphan_wiki_pages() -> list[str] | None:
    """
    Find wiki/ pages not referenced by any other note in the workspace.

    Scope is intentionally limited to wiki/ — directories like memory/, inbox/,
    context/, sessions/, and todos/ contain standalone notes by design and are
    not expected to be cross-linked.

    Returns None if wiki/ doesn't exist yet (not an error — just not built yet).
    """
    if not WIKI.exists():
        return None

    wiki_files = sorted(WIKI.glob('*.md'))
    if not wiki_files:
        return []

    # Build a corpus from all workspace .md files (excluding trash/tmp)
    exclude = {'trash', 'tmp', 'temp', 'docs-archive', '__pycache__', '.git', 'completed'}
    all_files = all_md_files(WORKSPACE, exclude=exclude)

    # Read all non-wiki files into a combined reference corpus
    corpus = ''
    for p in all_files:
        if p.parent == WIKI:
            continue  # don't count self-references within wiki/
        try:
            corpus += p.read_text(encoding='utf-8', errors='replace') + '\n'
        except Exception:
            pass

    orphans = []
    for p in wiki_files:
        stem = p.stem
        name = p.name
        patterns = [
            re.escape(stem),
            re.escape(name),
            re.escape(str(p.relative_to(WORKSPACE))),
        ]
        referenced = any(re.search(pat, corpus, re.IGNORECASE) for pat in patterns)
        if not referenced:
            orphans.append(str(p.relative_to(WORKSPACE)))

    return orphans


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(
    empty_related: list[str],
    stale_context: list[tuple[str, str]],
    dup_ids: list[tuple[str, list[str]]],
    orphans: list[str] | None,
) -> str:
    lines = [
        '---',
        f'title: Workspace Lint Report',
        f'date: {TODAY.isoformat()}',
        'tags: [lint, health]',
        '---',
        '',
        f'# Workspace Lint Report — {TODAY.isoformat()}',
        '',
    ]

    def section(title, items, fmt_fn):
        lines.append(f'## {title}')
        lines.append('')
        if not items:
            lines.append('_None — all clear._')
        else:
            for item in items:
                lines.append(fmt_fn(item))
        lines.append('')

    section(
        f'Review Notes with Empty `related:` ({len(empty_related)})',
        empty_related,
        lambda x: f'- `{x}`',
    )

    section(
        f'Stale Context Notes >90 days ({len(stale_context)})',
        stale_context,
        lambda x: f'- `{x[0]}` — last updated {x[1]}',
    )

    section(
        f'Duplicate Todo IDs ({len(dup_ids)})',
        dup_ids,
        lambda x: f'- ID `{x[0]}`: {", ".join(f"`{f}`" for f in x[1])}',
    )

    # Orphan wiki pages
    lines.append('## Orphan Wiki Pages')
    lines.append('')
    if orphans is None:
        lines.append('_`wiki/` not yet created — this check will activate once Todo 123 task 1 (wiki layer) is built._')
    elif not orphans:
        lines.append('_None — all clear._')
    else:
        for item in orphans:
            lines.append(f'- `{item}`')
    lines.append('')

    tracked = len(empty_related) + len(stale_context) + len(dup_ids) + (len(orphans) if orphans else 0)
    lines.append('---')
    lines.append(f'**{tracked} issue(s) found.** Generated by `lint.py`.')
    lines.append('')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print('[lint] Checking empty related fields...', flush=True)
    empty_related = check_empty_related()

    print('[lint] Checking stale context notes...', flush=True)
    stale_context = check_stale_context()

    print('[lint] Checking duplicate todo IDs...', flush=True)
    dup_ids = check_duplicate_todo_ids()

    print('[lint] Checking orphan wiki pages...', flush=True)
    orphans = check_orphan_wiki_pages()

    report = build_report(empty_related, stale_context, dup_ids, orphans)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report, encoding='utf-8')

    tracked = len(empty_related) + len(stale_context) + len(dup_ids) + (len(orphans) if orphans else 0)
    print(f'[lint] Done. {tracked} issue(s) found. Report: {REPORT}', flush=True)

    if '--summary' in sys.argv:
        print(report)


if __name__ == '__main__':
    main()
