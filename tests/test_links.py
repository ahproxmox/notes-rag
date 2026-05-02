"""Tests for links.py + /api/links/* endpoints."""

import os
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('OPENROUTER_API_KEY', 'test')

from features import links
from api import app

client = TestClient(app)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write(path: Path, content: str):
    path.write_text(content, encoding='utf-8')


def _note(title: str, body: str, extra_fm: dict | None = None) -> str:
    fm_lines = [f'title: "{title}"', 'date_created: 2026-01-01']
    if extra_fm:
        for k, v in extra_fm.items():
            if isinstance(v, list):
                items = '\n'.join(f'  - {x}' for x in v)
                fm_lines.append(f'{k}:\n{items}')
            else:
                fm_lines.append(f'{k}: {v}')
    return '---\n' + '\n'.join(fm_lines) + '\n---\n\n' + body + '\n'


FIXTURES = Path(__file__).parent.parent / 'fixtures' / 'supersession'


# ── Frontmatter round-trip ──────────────────────────────────────────────────

def test_serialise_round_trip(tmp_path):
    src = tmp_path / 'n.md'
    _write(src, _note('Hello', 'Body content here.'))
    fm, body = links.load_note(src)
    assert fm['title'] == 'Hello'
    assert 'Body content here.' in body
    # Re-serialise, parse again, confirm stable
    rewritten = links.serialise_note(fm, body)
    _write(src, rewritten)
    fm2, body2 = links.load_note(src)
    assert fm2['title'] == 'Hello'
    assert body.strip() == body2.strip()


# ── Exclusion set ───────────────────────────────────────────────────────────

def test_build_exclusion_set_gathers_all_fields():
    fm = {
        'supersedes': ['a.md', 'b.md'],
        'related': ['c.md'],
        'rejected_link_candidates': ['d.md'],
        'superseded_by': 'z.md',
    }
    ex = links.build_exclusion_set(fm, 'self.md')
    assert ex == {'self.md', 'a.md', 'b.md', 'c.md', 'd.md', 'z.md'}


def test_build_exclusion_set_handles_missing():
    ex = links.build_exclusion_set({}, 'self.md')
    assert ex == {'self.md'}


# ── LLM judge ───────────────────────────────────────────────────────────────

def _mock_llm(resp_text: str):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=resp_text)
    return llm


def test_judge_supersedes():
    llm = _mock_llm('{"relation": "supersedes", "reason": "B updates A"}')
    rel, reason = links.judge_relation('A content', 'B content', llm)
    assert rel == 'supersedes'
    assert 'updates' in reason


def test_judge_related():
    llm = _mock_llm('{"relation": "related", "reason": "same topic"}')
    rel, reason = links.judge_relation('A', 'B', llm)
    assert rel == 'related'


def test_judge_none_on_invalid_json():
    llm = _mock_llm('not json at all')
    rel, reason = links.judge_relation('A', 'B', llm)
    assert rel == 'none'


def test_judge_none_on_llm_error():
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError('boom')
    rel, _ = links.judge_relation('A', 'B', llm)
    assert rel == 'none'


def test_judge_truncates_inputs():
    llm = _mock_llm('{"relation": "none", "reason": "x"}')
    long = 'x' * 5000
    links.judge_relation(long, long, llm)
    prompt = llm.invoke.call_args[0][0]
    # Full 5000-char input must not all appear in the prompt
    assert prompt.count('x') < 5000


# ── scan_links orchestrator ─────────────────────────────────────────────────

def _make_doc(filename: str, similarity: float):
    doc = MagicMock()
    doc.metadata = {'filename': filename, 'similarity': similarity}
    return doc


def test_scan_returns_categorised_results(tmp_path):
    source = tmp_path / 'new.md'
    target = tmp_path / 'old.md'
    _write(source, _note('New', 'Body of new note.'))
    _write(target, _note('Old', 'Body of old note.'))

    retrieve = MagicMock(return_value=[_make_doc('old.md', 0.85)])
    llm = _mock_llm('{"relation": "supersedes", "reason": "B replaces A"}')

    def resolve(fname):
        return tmp_path / fname

    results = links.scan_links(source, resolve, retrieve, llm)
    assert len(results['supersessions']) == 1
    assert results['supersessions'][0]['path'] == 'old.md'
    assert results['related'] == []


def test_scan_skips_excluded_filenames(tmp_path):
    source = tmp_path / 'n.md'
    _write(source, _note('N', 'body', extra_fm={'rejected_link_candidates': ['skip.md']}))
    retrieve = MagicMock(return_value=[
        _make_doc('skip.md', 0.9),
        _make_doc('n.md', 0.9),       # self, also excluded
    ])
    llm = _mock_llm('{"relation": "supersedes", "reason": "x"}')
    results = links.scan_links(source, lambda f: tmp_path / f, retrieve, llm)
    assert results['supersessions'] == []
    assert results['related'] == []


def test_scan_respects_similarity_threshold(tmp_path):
    source = tmp_path / 'n.md'
    target = tmp_path / 'low.md'
    _write(source, _note('N', 'body'))
    _write(target, _note('Low', 'body'))
    retrieve = MagicMock(return_value=[_make_doc('low.md', 0.2)])
    llm = _mock_llm('{"relation": "supersedes", "reason": "x"}')
    results = links.scan_links(source, lambda f: tmp_path / f, retrieve, llm, threshold=0.6)
    assert results['supersessions'] == []
    llm.invoke.assert_not_called()


def test_scan_skips_already_superseded_candidate(tmp_path):
    source = tmp_path / 'new.md'
    target = tmp_path / 'old.md'
    _write(source, _note('N', 'body'))
    _write(target, _note('O', 'body', extra_fm={'superseded_by': 'other.md'}))
    retrieve = MagicMock(return_value=[_make_doc('old.md', 0.9)])
    llm = _mock_llm('{"relation": "supersedes", "reason": "x"}')
    results = links.scan_links(source, lambda f: tmp_path / f, retrieve, llm)
    assert results['supersessions'] == []
    llm.invoke.assert_not_called()


# ── Commit: supersedes ──────────────────────────────────────────────────────

def test_commit_supersedes_writes_both_sides(tmp_path):
    src = tmp_path / 'new.md'
    tgt = tmp_path / 'old.md'
    _write(src, _note('New', 'body'))
    _write(tgt, _note('Old', 'body'))
    src_fm, tgt_fm = links.commit_supersedes(src, tgt)
    assert tgt_fm['superseded_by'] == 'new.md'
    assert tgt_fm['superseded_at'] == date.today().isoformat()
    assert 'old.md' in src_fm['supersedes']

    # Re-read from disk to verify atomic write
    disk_src, _ = links.load_note(src)
    disk_tgt, _ = links.load_note(tgt)
    assert 'old.md' in disk_src['supersedes']
    assert disk_tgt['superseded_by'] == 'new.md'


def test_commit_supersedes_conflict_when_already_superseded(tmp_path):
    src = tmp_path / 'new.md'
    tgt = tmp_path / 'old.md'
    _write(src, _note('N', 'body'))
    _write(tgt, _note('O', 'body', extra_fm={'superseded_by': 'other.md'}))
    with pytest.raises(links.ConflictError):
        links.commit_supersedes(src, tgt)


def test_commit_supersedes_appends_to_existing_list(tmp_path):
    src = tmp_path / 'new.md'
    tgt_a = tmp_path / 'a.md'
    tgt_b = tmp_path / 'b.md'
    _write(src, _note('N', 'body', extra_fm={'supersedes': ['a.md']}))
    _write(tgt_a, _note('A', 'body', extra_fm={'superseded_by': 'new.md'}))
    _write(tgt_b, _note('B', 'body'))
    src_fm, _ = links.commit_supersedes(src, tgt_b)
    assert src_fm['supersedes'] == ['a.md', 'b.md']


# ── Commit: related ─────────────────────────────────────────────────────────

def test_commit_related_symmetric(tmp_path):
    a = tmp_path / 'a.md'
    b = tmp_path / 'b.md'
    _write(a, _note('A', 'body'))
    _write(b, _note('B', 'body'))
    a_fm, b_fm = links.commit_related(a, b)
    assert 'b.md' in a_fm['related']
    assert 'a.md' in b_fm['related']


def test_commit_related_idempotent(tmp_path):
    a = tmp_path / 'a.md'
    b = tmp_path / 'b.md'
    _write(a, _note('A', 'body', extra_fm={'related': ['b.md']}))
    _write(b, _note('B', 'body', extra_fm={'related': ['a.md']}))
    a_fm, b_fm = links.commit_related(a, b)
    assert a_fm['related'] == ['b.md']
    assert b_fm['related'] == ['a.md']


# ── Reject ──────────────────────────────────────────────────────────────────

def test_reject_appends_and_is_idempotent(tmp_path):
    src = tmp_path / 'n.md'
    _write(src, _note('N', 'body'))
    fm = links.reject_candidate(src, 'bad.md')
    assert fm['rejected_link_candidates'] == ['bad.md']
    fm = links.reject_candidate(src, 'bad.md')
    assert fm['rejected_link_candidates'] == ['bad.md']


# ── Atomic write pair ───────────────────────────────────────────────────────

def test_atomic_write_pair_rolls_back_a_on_b_failure(tmp_path):
    a = tmp_path / 'a.md'
    b = tmp_path / 'b.md'
    _write(a, 'A-ORIGINAL')
    _write(b, 'B-ORIGINAL')
    # Simulate B failing during replace by making b.md point at an invalid dir
    original_replace = os.replace
    call_count = [0]
    def fail_second(src_, dst_):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError('simulated failure on B')
        return original_replace(src_, dst_)
    with patch('links.os.replace', side_effect=fail_second):
        with pytest.raises(OSError):
            links.atomic_write_pair(a, 'A-NEW', b, 'B-NEW')
    assert a.read_text() == 'A-ORIGINAL'
    assert b.read_text() == 'B-ORIGINAL'


# ── Endpoint tests (path-patched) ───────────────────────────────────────────

@pytest.fixture
def note_env(tmp_path):
    (tmp_path / 'src.md').write_text(_note('Src', 'Source body.'), encoding='utf-8')
    (tmp_path / 'tgt.md').write_text(_note('Tgt', 'Target body.'), encoding='utf-8')

    def _find(fname: str):
        p = tmp_path / os.path.basename(fname)
        return p if p.exists() else None

    with patch('api._find_note', side_effect=_find):
        yield tmp_path


def test_confirm_supersedes_endpoint(note_env):
    with patch('api._reindex_paths'):
        resp = client.post('/api/links/confirm', json={
            'type': 'supersedes', 'source': 'src.md', 'target': 'tgt.md',
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data['target_fm']['superseded_by'] == 'src.md'
    assert 'tgt.md' in data['source_fm']['supersedes']


def test_confirm_supersedes_conflict(note_env):
    # Pre-seed target as already superseded
    (note_env / 'tgt.md').write_text(
        _note('Tgt', 'body', extra_fm={'superseded_by': 'other.md'}), encoding='utf-8')
    with patch('api._reindex_paths'):
        resp = client.post('/api/links/confirm', json={
            'type': 'supersedes', 'source': 'src.md', 'target': 'tgt.md',
        })
    assert resp.status_code == 409


def test_confirm_related_endpoint(note_env):
    with patch('api._reindex_paths'):
        resp = client.post('/api/links/confirm', json={
            'type': 'related', 'source': 'src.md', 'target': 'tgt.md',
        })
    assert resp.status_code == 200
    data = resp.json()
    assert 'tgt.md' in data['source_fm']['related']
    assert 'src.md' in data['target_fm']['related']


def test_confirm_rejects_unknown_type(note_env):
    with patch('api._reindex_paths'):
        resp = client.post('/api/links/confirm', json={
            'type': 'bogus', 'source': 'src.md', 'target': 'tgt.md',
        })
    assert resp.status_code == 400


def test_reject_endpoint_persists(note_env):
    resp = client.post('/api/links/reject',
                       json={'source': 'src.md', 'target': 'tgt.md'})
    assert resp.status_code == 200
    fm, _ = links.load_note(note_env / 'src.md')
    assert fm['rejected_link_candidates'] == ['tgt.md']


def test_reject_endpoint_404_on_missing_source(tmp_path):
    with patch('api._find_note', return_value=None):
        resp = client.post('/api/links/reject',
                           json={'source': 'missing.md', 'target': 'x.md'})
    assert resp.status_code == 404
