import os
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

os.environ.setdefault('OPENROUTER_API_KEY', 'test')

from api import app

client = TestClient(app)

NOTE_CONTENT = """\
---
title: "Test Note"
date_created: 2026-01-01
reviewed: true
tags: [test]
---

This is the note body.
Second line.
"""


def _write_note(path: Path, content: str):
    path.write_text(content, encoding='utf-8')


# ── POST /api/notes/search ────────────────────────────────────────────────────

def test_note_search_deduplicates_chunks(tmp_path):
    fake_chunks = [
        {'source': 'note-a.md', 'content': 'First chunk of note A', 'score': 0.9},
        {'source': 'note-a.md', 'content': 'Second chunk of note A', 'score': 0.7},
        {'source': 'note-b.md', 'content': 'Only chunk of note B', 'score': 0.8},
    ]
    with patch('api.search', return_value=('', ['note-a.md', 'note-b.md'], fake_chunks)):
        with patch('api._notes_dir', tmp_path):
            resp = client.post('/api/notes/search', json={'query': 'test query'})
    assert resp.status_code == 200
    results = resp.json()['results']
    assert len(results) == 2


def test_note_search_ranks_by_best_chunk_score(tmp_path):
    fake_chunks = [
        {'source': 'low.md',  'content': 'low relevance', 'score': 0.3},
        {'source': 'high.md', 'content': 'high relevance', 'score': 0.95},
    ]
    with patch('api.search', return_value=('', ['low.md', 'high.md'], fake_chunks)):
        with patch('api._notes_dir', tmp_path):
            resp = client.post('/api/notes/search', json={'query': 'anything'})
    results = resp.json()['results']
    assert results[0]['filename'] == 'high.md'
    assert results[0]['score'] == 0.95


def test_note_search_extracts_title_from_frontmatter(tmp_path):
    _write_note(tmp_path / 'my-note.md', NOTE_CONTENT)
    fake_chunks = [{'source': 'my-note.md', 'content': 'body text', 'score': 0.8}]
    with patch('api.search', return_value=('', ['my-note.md'], fake_chunks)):
        with patch('api._notes_dir', tmp_path):
            resp = client.post('/api/notes/search', json={'query': 'body'})
    results = resp.json()['results']
    assert results[0]['title'] == 'Test Note'


# ── GET /api/notes/{filename} ────────────────────────────────────────────────

def test_note_get_returns_body_without_frontmatter(tmp_path):
    _write_note(tmp_path / 'test-note.md', NOTE_CONTENT)
    with patch('api._notes_dir', tmp_path):
        resp = client.get('/api/notes/test-note.md')
    assert resp.status_code == 200
    data = resp.json()
    assert data['title'] == 'Test Note'
    assert 'This is the note body.' in data['body']
    assert '---' not in data['body']


def test_note_get_404_for_missing_file(tmp_path):
    with patch('api._notes_dir', tmp_path):
        resp = client.get('/api/notes/nonexistent.md')
    assert resp.status_code == 404


def test_note_get_rejects_non_md(tmp_path):
    with patch('api._notes_dir', tmp_path):
        resp = client.get('/api/notes/evil.sh')
    assert resp.status_code == 400


# ── PATCH /api/notes/{filename} ───────────────────────────────────────────────

def test_note_patch_writes_updated_body(tmp_path):
    _write_note(tmp_path / 'test-note.md', NOTE_CONTENT)
    with patch('api._notes_dir', tmp_path):
        resp = client.patch('/api/notes/test-note.md', json={'body': 'Updated body content.'})
    assert resp.status_code == 200
    updated = (tmp_path / 'test-note.md').read_text(encoding='utf-8')
    assert 'Updated body content.' in updated


def test_note_patch_sets_reviewed_unreviewed(tmp_path):
    _write_note(tmp_path / 'test-note.md', NOTE_CONTENT)
    with patch('api._notes_dir', tmp_path):
        client.patch('/api/notes/test-note.md', json={'body': 'New body.'})
    updated = (tmp_path / 'test-note.md').read_text(encoding='utf-8')
    assert 'reviewed: unreviewed' in updated
    assert 'reviewed: true' not in updated


def test_note_patch_adds_updated_date(tmp_path):
    _write_note(tmp_path / 'test-note.md', NOTE_CONTENT)
    with patch('api._notes_dir', tmp_path):
        client.patch('/api/notes/test-note.md', json={'body': 'New body.'})
    updated = (tmp_path / 'test-note.md').read_text(encoding='utf-8')
    assert 'updated:' in updated


def test_note_patch_rejects_path_traversal(tmp_path):
    with patch('api._notes_dir', tmp_path):
        resp = client.patch('/api/notes/../etc/passwd', json={'body': 'evil'})
    assert resp.status_code in (400, 404)


def test_note_patch_404_for_missing_file(tmp_path):
    with patch('api._notes_dir', tmp_path):
        resp = client.patch('/api/notes/ghost.md', json={'body': 'anything'})
    assert resp.status_code == 404
