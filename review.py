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

    end = content.find('---', 3)
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
            lines.append(f"{k}: {v}")
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
