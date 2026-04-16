import os
import pytest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('OPENROUTER_API_KEY', 'test-key')

from fastapi.testclient import TestClient
from api import app

client = TestClient(app)


def _write_todo(path: Path, content: str):
    path.write_text(content, encoding='utf-8')


TARGET = """\
---
id: 10
title: "Deploy service"
status: pending
swimlane: Dev
---
Deploy the service to production.
"""

CANDIDATE_SAME_LANE = """\
---
id: 11
title: "Write tests"
status: pending
swimlane: Dev
---
Write the unit tests first.
"""

CANDIDATE_DIFF_LANE = """\
---
id: 12
title: "Update docs"
status: pending
swimlane: Infra
---
Update the documentation.
"""

CANDIDATE_COMPLETED = """\
---
id: 13
title: "Already done"
status: completed
swimlane: Dev
---
This is finished.
"""


def test_prereqs_returns_404_for_missing_todo(tmp_path):
    with patch('api._todos_dir', tmp_path):
        resp = client.post('/api/kanban/todos/999/prereqs')
    assert resp.status_code == 404


def test_prereqs_returns_empty_when_no_candidates(tmp_path):
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    with patch('api._todos_dir', tmp_path):
        resp = client.post('/api/kanban/todos/10/prereqs')
    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': []}


def test_prereqs_excludes_different_swimlane(tmp_path):
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    _write_todo(tmp_path / '012-update-docs.md', CANDIDATE_DIFF_LANE)
    with patch('api._todos_dir', tmp_path):
        resp = client.post('/api/kanban/todos/10/prereqs')
    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': []}


def test_prereqs_excludes_completed_todos(tmp_path):
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    _write_todo(tmp_path / '013-already-done.md', CANDIDATE_COMPLETED)
    with patch('api._todos_dir', tmp_path):
        resp = client.post('/api/kanban/todos/10/prereqs')
    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': []}


def test_prereqs_calls_llm_with_same_lane_candidates_and_writes_frontmatter(tmp_path):
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    _write_todo(tmp_path / '011-write-tests.md', CANDIDATE_SAME_LANE)
    _write_todo(tmp_path / '012-update-docs.md', CANDIDATE_DIFF_LANE)

    with patch('api._todos_dir', tmp_path):
        with patch('api.search', return_value=('Tests must pass before deploy', [], [])):
            with patch('api._call_openrouter', return_value='[11]') as mock_llm:
                resp = client.post('/api/kanban/todos/10/prereqs')

    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': [11]}

    # LLM prompt should mention the candidate but not the diff-lane todo
    prompt = mock_llm.call_args[0][0]
    assert '11' in prompt
    assert '12' not in prompt

    # frontmatter written
    updated = (tmp_path / '010-deploy-service.md').read_text()
    assert 'prereqIds: [11]' in updated


def test_prereqs_rejects_ids_not_in_candidate_list(tmp_path):
    """LLM hallucinating an ID that isn't a real candidate should be filtered out."""
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    _write_todo(tmp_path / '011-write-tests.md', CANDIDATE_SAME_LANE)

    with patch('api._todos_dir', tmp_path):
        with patch('api.search', return_value=('', [], [])):
            with patch('api._call_openrouter', return_value='[11, 999]'):
                resp = client.post('/api/kanban/todos/10/prereqs')

    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': [11]}  # 999 filtered out


def test_prereqs_handles_empty_llm_response(tmp_path):
    _write_todo(tmp_path / '010-deploy-service.md', TARGET)
    _write_todo(tmp_path / '011-write-tests.md', CANDIDATE_SAME_LANE)

    with patch('api._todos_dir', tmp_path):
        with patch('api.search', return_value=('', [], [])):
            with patch('api._call_openrouter', return_value='[]'):
                resp = client.post('/api/kanban/todos/10/prereqs')

    assert resp.status_code == 200
    assert resp.json() == {'prereqIds': []}
