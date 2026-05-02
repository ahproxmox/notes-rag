"""Wing/room classification for indexed documents.

Loaded once at startup from wings.yaml. classify_document() takes a
workspace-relative path + file content and returns (wing, room) tuples.

Classification strategy:
  1. path_patterns (glob) — if any matches, that wing wins immediately
  2. slug keywords (weight 3) — filename stem scored against keywords
  3. body keywords (weight 1) — first 500 chars scored against keywords
Highest-scoring wing wins. Ties or no matches → wing='unknown', room=None.
"""

import os
import re
import fnmatch
import yaml
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = os.environ.get(
    'WINGS_CONFIG_PATH',
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'wings.yaml'),
)

_SLUG_WEIGHT = 3
_BODY_WEIGHT = 1
_BODY_SCAN_CHARS = 500


@lru_cache(maxsize=1)
def load_wings_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {'wings': {}}


def _slug_from_filename(filename: str) -> str:
    """Strip date prefix + extension, return lowercase slug.

    '2026-04-08-09-ct124-ram-fix.md' -> 'ct124-ram-fix'
    '001-batch-research-links.md'    -> 'batch-research-links'
    'opnsense-api.md'                -> 'opnsense-api'
    """
    stem = Path(filename).stem.lower()
    # Strip leading date like 2026-04-08-09- or number prefixes like 001-
    stem = re.sub(r'^\d{4}-\d{2}-\d{2}(-\d{1,2})?-', '', stem)
    stem = re.sub(r'^\d{1,4}-', '', stem)
    return stem


def _score_keywords(keywords: list, slug: str, body_lower: str) -> int:
    score = 0
    for kw in keywords or []:
        kw_l = kw.lower()
        if kw_l in slug:
            score += _SLUG_WEIGHT
        if kw_l in body_lower:
            score += _BODY_WEIGHT
    return score


def _match_path(rel_path: str, patterns: list) -> bool:
    for pat in patterns or []:
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def classify_document(rel_path: str, filename: str, content: str) -> tuple[str, str | None]:
    """Classify a document into (wing, room).

    Args:
        rel_path: path relative to the workspace root (e.g. 'sessions/2026-04-08-ct124-ram-fix.md')
        filename: basename of the file
        content: file content (only first 500 chars used)

    Returns:
        (wing, room) — wing is always a string ('unknown' if nothing matched),
        room is a string or None.
    """
    cfg = load_wings_config()
    wings_cfg = cfg.get('wings', {})

    slug = _slug_from_filename(filename)
    body_lower = (content or '')[:_BODY_SCAN_CHARS].lower()

    # Pass 1: path patterns — first match wins
    for wing_name, wing_def in wings_cfg.items():
        if _match_path(rel_path, wing_def.get('path_patterns', [])):
            room = _best_room(wing_def, slug, body_lower)
            # Special case: files under todos/ always use room='todos'
            if rel_path.startswith('todos/'):
                room = 'todos'
            return wing_name, room

    # Pass 2: keyword scoring across all wings
    scores: dict[str, int] = {}
    for wing_name, wing_def in wings_cfg.items():
        scores[wing_name] = _score_keywords(wing_def.get('keywords', []), slug, body_lower)

    best_wing = max(scores, key=scores.get) if scores else None
    if not best_wing or scores[best_wing] == 0:
        return 'unknown', None

    room = _best_room(wings_cfg[best_wing], slug, body_lower)
    return best_wing, room


def _best_room(wing_def: dict, slug: str, body_lower: str) -> str | None:
    rooms = wing_def.get('rooms', {}) or {}
    if not rooms:
        return None
    room_scores = {
        name: _score_keywords(rdef.get('keywords', []), slug, body_lower)
        for name, rdef in rooms.items()
    }
    best = max(room_scores, key=room_scores.get) if room_scores else None
    if not best or room_scores[best] == 0:
        return None
    return best


if __name__ == '__main__':
    # Self-test against a handful of known files.
    samples = [
        ('sessions/2026-03-28-01-opnsense-caddy.md', '## Summary\nDeployed Caddy with wildcard TLS for internal.ahproxmox-claude.cc'),
        ('sessions/2026-04-08-09-ct124-ram-fix.md',  '## Summary\nOrphaned github-mcp-server processes were eating RAM on CT 124.'),
        ('sessions/2026-03-24-22-rag-phase2-deployed.md', '## Summary\nMigrated RAG from ChromaDB + fts.db to unified rag.db with sqlite-vec and fastembed.'),
        ('sessions/2026-04-09-10-openclaw-model-fixes.md', '## Summary\nUpdated openclaw models.json and exec-approvals.json'),
        ('sessions/2026-03-29-13-pickleball-infra-prep.md', '## Summary\nPickleball playwright selectors'),
        ('sessions/2026-04-07-14-kanban-height-fix-browser-testing.md', '## Summary\nFixed kanban CSS height issue via chrome-browse'),
        ('sessions/2026-04-03-10-manual-notes-workflow.md', '## Summary\nManual notes workflow: watcher injects frontmatter, curator reviews queue'),
        ('context/mcp-permissions.md', 'MCP run_command whitelist'),
        ('context/obsidian-mounts.md', 'Obsidian vault mounts on CT 104'),
        ('todos/017-hashicorp-vault-self-hosted-secrets-management.md', '## Summary\nSelf-host Vault'),
        ('SOUL.md', 'Who you are'),
    ]
    for path, content in samples:
        filename = os.path.basename(path)
        wing, room = classify_document(path, filename, content)
        print(f'{wing:10} {str(room):20} {path}')
