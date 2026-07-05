import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('OPENROUTER_API_KEY', 'test-key')

from fastapi.testclient import TestClient
from api import app

client = TestClient(app)


def _write_todo(path: Path, content: str):
    path.write_text(content, encoding='utf-8')


NO_OPTIONAL_FIELDS = """\
---
id: 20
title: "Harden parser"
status: pending
priority: medium
created: 2026-07-06
---
Notes from implementation.

## Deviations
swimlane: Infra
project: wedding
assignee: openclaw
completed: 2026-01-01
prereqs: [12, 14]
"""

BODY_SWIMLANE_ONLY = """\
---
id: 21
title: "Drag me"
status: pending
priority: low
created: 2026-07-06
---
## Notes
swimlane: OldNote
"""


def test_body_fields_do_not_leak_into_missing_frontmatter_fields(tmp_path):
    _write_todo(tmp_path / '020-harden-parser.md', NO_OPTIONAL_FIELDS)
    with patch('api.app._todos_dir', tmp_path):
        resp = client.get('/api/kanban/todos/20')
    assert resp.status_code == 200
    todo = resp.json()
    assert todo['swimlane'] is None
    assert todo['project'] is None
    assert todo['assignee'] is None
    assert todo['completed'] is None


def test_body_prereqs_do_not_create_phantom_links(tmp_path):
    _write_todo(tmp_path / '020-harden-parser.md', NO_OPTIONAL_FIELDS)
    with patch('api.app._todos_dir', tmp_path):
        resp = client.get('/api/kanban/todos/20')
    assert resp.status_code == 200
    assert resp.json()['prereqIds'] == []


def test_patch_swimlane_updates_frontmatter_not_body(tmp_path):
    path = tmp_path / '021-drag-me.md'
    _write_todo(path, BODY_SWIMLANE_ONLY)
    with patch('api.app._todos_dir', tmp_path):
        resp = client.patch('/api/kanban/todos/21', json={'swimlane': 'Dev'})
    assert resp.status_code == 200
    assert resp.json()['swimlane'] == 'Dev'
    content = path.read_text(encoding='utf-8')
    fm, body = content.split('---\n', 2)[1], content.split('---\n', 2)[2]
    assert 'swimlane: Dev' in fm
    assert 'swimlane: OldNote' in body


def test_frontmatter_fields_still_parse_normally(tmp_path):
    _write_todo(tmp_path / '022-normal.md', """\
---
id: 22
title: "Normal todo"
status: in_progress
priority: high
created: 2026-07-06
swimlane: Dev
project: notes-rag
prereqs: [20]
---
Body text.
""")
    with patch('api.app._todos_dir', tmp_path):
        resp = client.get('/api/kanban/todos/22')
    assert resp.status_code == 200
    todo = resp.json()
    assert todo['swimlane'] == 'Dev'
    assert todo['project'] == 'notes-rag'
    assert todo['status'] == 'in_progress'
    assert todo['prereqIds'] == ['20']
