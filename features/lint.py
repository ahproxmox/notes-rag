#!/usr/bin/env python3
"""
Workspace lint / health-check script.
Writes a report to /mnt/Claude/inbox/lint-report.md

Checks:
  1. Review notes with empty related: [] field
  2. Stale context notes (no update in >90 days)
  3. Duplicate todo IDs
  4. Orphan wiki pages (wiki/ only — standalone dirs like memory/, inbox/ are exempt by design)
  5. Stale wiki pages (source file modified after page was generated)
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
    """Return a dict of simple (non-list) frontmatter fields, or {} if none."""
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
        if ':' in line and not line.startswith(' ') and not line.startswith('-'):
            k, _, v = line.partition(':')
            fields[k.strip()] = v.strip()
    return fields


def parse_frontmatter_list(path: Path, field: str) -> list[str]:
    """Extract a YAML list field from frontmatter, e.g. sources: [f1, f2] or block style."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return []
    if not text.startswith('---'):
        return []
    end = text.find('---', 3)
    if end == -1:
        return []
    fm_text = text[3:end]

    # Inline style: sources: [a, b, c]
    inline = re.search(rf'^{re.escape(field)}:\s*\[(.+?)\]', fm_text, re.MULTILINE)
    if inline:
        return [s.strip().strip('"\'') for s in inline.group(1).split(',') if s.strip()]

    # Block style: sources:\n  - a\n  - b
    block = re.search(rf'^{re.escape(field)}:\s*\n((?:[ \t]+-[^\n]*\n?)+)', fm_text, re.MULTILINE)
    if block:
        items = []
        for line in block.group(1).splitlines():
            m = re.match(r'^\s+-\s+(.+)', line)
            if m:
                items.append(m.group(1).strip())
        return items

    return []


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


def _find_source_file(filename: str) -> Path | None:
    """Locate a source file by name across workspace and Obsidian.

    Excludes wiki/ to avoid finding the wiki page itself as its own source.
    """
    for root in (WORKSPACE, OBSIDIAN):
        matches = [p for p in root.rglob(filename) if p.parent != WIKI]
        if matches:
            return matches[0]
    return None


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

    exclude = {'trash', 'tmp', 'temp', 'docs-archive', '__pycache__', '.git', 'completed'}
    all_files = all_md_files(WORKSPACE, exclude=exclude)

    corpus = ''
    for p in all_files:
        if p.parent == WIKI:
            continue
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
# Check 5: Stale wiki pages
# ---------------------------------------------------------------------------

def check_stale_wiki_pages() -> list[tuple[str, str, list[str]]] | None:
    """Flag wiki pages where any source file has been modified after generated date.

    Returns list of (wiki_page, generated_date, [stale_sources]).
    Returns None if wiki/ doesn't exist yet.
    """
    if not WIKI.exists():
        return None

    wiki_files = sorted(WIKI.glob('*.md'))
    if not wiki_files:
        return []

    stale = []
    for p in wiki_files:
        fm = parse_frontmatter(p)
        generated_str = fm.get('generated', '').strip()
        generated = parse_date(generated_str)
        if not generated:
            continue  # no generated date — skip

        # Compare by date only — a source modified on any day after generated is stale.
        # Using date comparison avoids timestamp precision issues (generated: has no time).
        sources = parse_frontmatter_list(p, 'sources')
        stale_sources = []
        for fname in sources:
            src_path = _find_source_file(fname)
            if src_path:
                src_date = date.fromtimestamp(src_path.stat().st_mtime)
                if src_date > generated:
                    stale_sources.append(fname)

        if stale_sources:
            stale.append((p.name, generated_str, stale_sources))

    return stale


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(
    empty_related: list[str],
    stale_context: list[tuple[str, str]],
    dup_ids: list[tuple[str, list[str]]],
    orphans: list[str] | None,
    stale_wiki: list[tuple[str, str, list[str]]] | None,
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
        lines.append('_`wiki/` not yet created._')
    elif not orphans:
        lines.append('_None — all clear._')
    else:
        for item in orphans:
            lines.append(f'- `{item}`')
    lines.append('')

    # Stale wiki pages
    lines.append('## Stale Wiki Pages (source updated after generation)')
    lines.append('')
    if stale_wiki is None:
        lines.append('_`wiki/` not yet created._')
    elif not stale_wiki:
        lines.append('_None — all clear._')
    else:
        for wiki_name, gen_date, stale_sources in stale_wiki:
            lines.append(f'- `wiki/{wiki_name}` (generated {gen_date}) — stale sources: {", ".join(f"`{s}`" for s in stale_sources)}')
    lines.append('')

    tracked = (
        len(empty_related)
        + len(stale_context)
        + len(dup_ids)
        + (len(orphans) if orphans else 0)
        + (len(stale_wiki) if stale_wiki else 0)
    )
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

    print('[lint] Checking stale wiki pages...', flush=True)
    stale_wiki = check_stale_wiki_pages()

    report = build_report(empty_related, stale_context, dup_ids, orphans, stale_wiki)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report, encoding='utf-8')

    tracked = (
        len(empty_related)
        + len(stale_context)
        + len(dup_ids)
        + (len(orphans) if orphans else 0)
        + (len(stale_wiki) if stale_wiki else 0)
    )
    print(f'[lint] Done. {tracked} issue(s) found. Report: {REPORT}', flush=True)

    if '--summary' in sys.argv:
        print(report)


if __name__ == '__main__':
    main()
