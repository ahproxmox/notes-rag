import math
import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# compute_decay_factor
# ---------------------------------------------------------------------------

def test_decay_factor_none_returns_1():
    from features.lifecycle import compute_decay_factor
    assert compute_decay_factor(None) == 1.0


def test_decay_factor_empty_returns_1():
    from features.lifecycle import compute_decay_factor
    assert compute_decay_factor('') == 1.0


def test_decay_factor_future_returns_1():
    from features.lifecycle import compute_decay_factor
    future = (date.today() + timedelta(days=10)).isoformat()
    assert compute_decay_factor(future) == 1.0


def test_decay_factor_today_returns_1():
    from features.lifecycle import compute_decay_factor
    assert compute_decay_factor(date.today().isoformat()) == 1.0


def test_decay_factor_180_days_approx_0_6():
    from features.lifecycle import compute_decay_factor
    past = (date.today() - timedelta(days=180)).isoformat()
    factor = compute_decay_factor(past)
    assert 0.58 <= factor <= 0.62


def test_decay_factor_365_days_below_0_4():
    from features.lifecycle import compute_decay_factor
    past = (date.today() - timedelta(days=365)).isoformat()
    assert compute_decay_factor(past) < 0.40


def test_decay_factor_30_days_above_0_9():
    from features.lifecycle import compute_decay_factor
    past = (date.today() - timedelta(days=30)).isoformat()
    assert compute_decay_factor(past) > 0.90


def test_decay_factor_bad_string_returns_1():
    from features.lifecycle import compute_decay_factor
    assert compute_decay_factor('not-a-date') == 1.0


def test_decay_factor_datetime_string_uses_date_prefix():
    """Datetime strings like '2025-01-01T12:00:00' should work via [:10] slice."""
    from features.lifecycle import compute_decay_factor
    past = (date.today() - timedelta(days=180)).isoformat() + 'T12:00:00'
    factor = compute_decay_factor(past)
    assert 0.58 <= factor <= 0.62


# ---------------------------------------------------------------------------
# confidence_for_folder
# ---------------------------------------------------------------------------

def test_confidence_context_is_max():
    from features.lifecycle import confidence_for_folder
    assert confidence_for_folder('context') == 1.0


def test_confidence_inbox_is_lower_than_context():
    from features.lifecycle import confidence_for_folder
    assert confidence_for_folder('inbox') < confidence_for_folder('context')


def test_confidence_inbox_is_lower_than_sessions():
    from features.lifecycle import confidence_for_folder
    assert confidence_for_folder('inbox') < confidence_for_folder('sessions')


def test_confidence_todos_below_context():
    from features.lifecycle import confidence_for_folder
    assert confidence_for_folder('todos') < confidence_for_folder('context')


def test_confidence_unknown_folder_uses_default():
    from features.lifecycle import confidence_for_folder, CONFIDENCE_DEFAULT
    assert confidence_for_folder('unknown-xyz-folder') == CONFIDENCE_DEFAULT


def test_confidence_all_values_in_range():
    from features.lifecycle import CONFIDENCE_BY_FOLDER
    for folder, weight in CONFIDENCE_BY_FOLDER.items():
        assert 0.0 < weight <= 1.0, f'{folder} confidence {weight} out of range'


# ---------------------------------------------------------------------------
# recalculate_lifecycle
# ---------------------------------------------------------------------------

def test_recalculate_lifecycle_updates_all_sources():
    from features.lifecycle import recalculate_lifecycle

    mock_store = MagicMock()
    mock_store._conn.execute.return_value.fetchall.return_value = [
        ('/mnt/Claude/context/foo.md', 'context', '2026-01-01'),
        ('/mnt/Claude/inbox/bar.md', 'inbox', '2025-01-01'),
        ('/mnt/Claude/todos/baz.md', 'todos', None),
    ]

    result = recalculate_lifecycle(mock_store)

    assert result['sources_updated'] == 3
    assert mock_store._conn.commit.called
    update_calls = [
        c for c in mock_store._conn.execute.call_args_list
        if 'UPDATE' in str(c)
    ]
    assert len(update_calls) == 3


def test_recalculate_lifecycle_uses_correct_confidence():
    from features.lifecycle import recalculate_lifecycle, CONFIDENCE_BY_FOLDER

    captured_updates = []

    def mock_execute(sql, params=None):
        m = MagicMock()
        if params and 'UPDATE' in sql:
            captured_updates.append(params)
            m.fetchall.return_value = []
        else:
            m.fetchall.return_value = [
                ('/mnt/Claude/context/foo.md', 'context', '2026-04-01'),
            ]
        return m

    mock_store = MagicMock()
    mock_store._conn.execute.side_effect = mock_execute

    recalculate_lifecycle(mock_store)

    assert len(captured_updates) == 1
    confidence, _decay, _source = captured_updates[0]
    assert confidence == CONFIDENCE_BY_FOLDER['context']


def test_recalculate_lifecycle_null_last_updated_gives_decay_1():
    from features.lifecycle import recalculate_lifecycle

    captured_updates = []

    def mock_execute(sql, params=None):
        m = MagicMock()
        if params and 'UPDATE' in sql:
            captured_updates.append(params)
            m.fetchall.return_value = []
        else:
            m.fetchall.return_value = [
                ('/mnt/Claude/todos/task.md', 'todos', None),
            ]
        return m

    mock_store = MagicMock()
    mock_store._conn.execute.side_effect = mock_execute

    recalculate_lifecycle(mock_store)

    _confidence, decay, _source = captured_updates[0]
    assert decay == 1.0


# ---------------------------------------------------------------------------
# Lifecycle multiplier in _retrieve
# ---------------------------------------------------------------------------

def _make_doc(content='test content', confidence=1.0, decay_factor=1.0,
              superseded_by=None, source='a.md', similarity=0.9):
    from langchain_core.documents import Document
    return Document(
        page_content=content,
        metadata={
            'source': source,
            'filename': source,
            'folder': 'context',
            'headers': '',
            'wing': 'infra',
            'room': 'rag',
            'project': None,
            'confidence': confidence,
            'decay_factor': decay_factor,
            'superseded_by': superseded_by,
            'similarity': similarity,
        },
    )


def test_lifecycle_multiplier_sets_lifecycle_score():
    from core.search import _retrieve

    doc = _make_doc(confidence=0.9, decay_factor=0.8)

    with patch('search.get_store') as mock_gs:
        mock_store = MagicMock()
        mock_store.search_bm25.return_value = [doc]
        mock_store.search_vector.return_value = [doc]
        mock_gs.return_value = mock_store

        results = _retrieve('test query', k=5)

    assert results, 'No results returned'
    lifecycle = results[0].metadata['lifecycle_score']
    assert abs(lifecycle - 0.72) < 0.001  # 0.9 × 0.8


def test_lifecycle_multiplier_1_0_for_fresh_confident_doc():
    from core.search import _retrieve

    doc = _make_doc(confidence=1.0, decay_factor=1.0)

    with patch('search.get_store') as mock_gs:
        mock_store = MagicMock()
        mock_store.search_bm25.return_value = [doc]
        mock_store.search_vector.return_value = [doc]
        mock_gs.return_value = mock_store

        results = _retrieve('test', k=5)

    assert results[0].metadata['lifecycle_score'] == 1.0


def test_lifecycle_multiplier_demotes_low_confidence():
    from core.search import _retrieve

    doc_high = _make_doc(content='high confidence content', confidence=1.0,
                         decay_factor=1.0, source='high.md')
    doc_low  = _make_doc(content='low confidence content',  confidence=0.7,
                         decay_factor=1.0, source='low.md')

    with patch('search.get_store') as mock_gs:
        mock_store = MagicMock()
        mock_store.search_bm25.return_value  = [doc_high, doc_low]
        mock_store.search_vector.return_value = [doc_high, doc_low]
        mock_gs.return_value = mock_store

        results = _retrieve('test', k=5)

    score_map = {r.metadata['filename']: r.metadata['rrf_score'] for r in results}
    assert score_map['high.md'] > score_map['low.md']


def test_lifecycle_multiplier_demotes_old_docs():
    from core.search import _retrieve

    doc_fresh = _make_doc(content='fresh document text', confidence=1.0,
                          decay_factor=1.0, source='fresh.md')
    doc_old   = _make_doc(content='old document text',   confidence=1.0,
                          decay_factor=0.4, source='old.md')

    with patch('search.get_store') as mock_gs:
        mock_store = MagicMock()
        mock_store.search_bm25.return_value  = [doc_fresh, doc_old]
        mock_store.search_vector.return_value = [doc_fresh, doc_old]
        mock_gs.return_value = mock_store

        results = _retrieve('test', k=5)

    score_map = {r.metadata['filename']: r.metadata['rrf_score'] for r in results}
    assert score_map['fresh.md'] > score_map['old.md']


def test_lifecycle_multiplier_missing_fields_default_to_1():
    """Chunks without lifecycle metadata (pre-migration) should get multiplier=1."""
    from core.search import _retrieve
    from langchain_core.documents import Document

    doc = Document(
        page_content='legacy chunk with no lifecycle fields',
        metadata={'source': 'old.md', 'filename': 'old.md', 'folder': 'context',
                  'headers': '', 'wing': None, 'room': None, 'project': None},
    )

    with patch('search.get_store') as mock_gs:
        mock_store = MagicMock()
        mock_store.search_bm25.return_value = [doc]
        mock_store.search_vector.return_value = []
        mock_gs.return_value = mock_store

        results = _retrieve('test', k=5)

    assert results[0].metadata['lifecycle_score'] == 1.0


# ---------------------------------------------------------------------------
# supersession_sweep
# ---------------------------------------------------------------------------

def test_supersession_sweep_skips_when_no_wing():
    from features.lifecycle import supersession_sweep

    mock_store = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = (None, None, '2026-01-01')
    mock_store._conn.execute.return_value = cursor

    result = supersession_sweep(mock_store, '/mnt/Claude/context/foo.md')
    assert result == []
    mock_store._conn.commit.assert_not_called()


def test_supersession_sweep_skips_when_no_similar_docs():
    from features.lifecycle import supersession_sweep

    mock_store = MagicMock()

    cursor_wing = MagicMock()
    cursor_wing.fetchone.return_value = ('infra', 'rag', '2026-01-01')

    cursor_chunk = MagicMock()
    cursor_chunk.fetchone.return_value = ('Some chunk content here',)

    mock_store._conn.execute.side_effect = [cursor_wing, cursor_chunk]
    mock_store.search_vector.return_value = []

    result = supersession_sweep(mock_store, '/mnt/Claude/context/foo.md')
    assert result == []
    mock_store._conn.commit.assert_not_called()


def test_supersession_sweep_skips_below_threshold():
    from features.lifecycle import supersession_sweep
    from langchain_core.documents import Document

    mock_store = MagicMock()

    cursor_wing = MagicMock()
    cursor_wing.fetchone.return_value = ('infra', 'rag', '2026-04-01')

    cursor_chunk = MagicMock()
    cursor_chunk.fetchone.return_value = ('chunk content',)

    mock_store._conn.execute.side_effect = [cursor_wing, cursor_chunk]

    # Similarity below threshold (0.92)
    similar_doc = Document(
        page_content='similar content',
        metadata={'source': '/mnt/Claude/context/old.md', 'similarity': 0.85},
    )
    mock_store.search_vector.return_value = [similar_doc]

    result = supersession_sweep(mock_store, '/mnt/Claude/context/foo.md')
    assert result == []
    mock_store._conn.commit.assert_not_called()


def test_supersession_sweep_marks_older_as_superseded():
    from features.lifecycle import supersession_sweep
    from langchain_core.documents import Document

    source = '/mnt/Claude/context/new-foo.md'
    other_source = '/mnt/Claude/context/old-foo.md'

    mock_store = MagicMock()

    cursor_wing = MagicMock()
    cursor_wing.fetchone.return_value = ('infra', 'rag', '2026-04-01')

    cursor_chunk = MagicMock()
    cursor_chunk.fetchone.return_value = ('Architecture overview content',)

    cursor_other_date = MagicMock()
    cursor_other_date.fetchone.return_value = ('2025-01-01',)

    cursor_update = MagicMock()

    mock_store._conn.execute.side_effect = [
        cursor_wing,        # SELECT wing, room, last_updated
        cursor_chunk,       # SELECT content LIMIT 1
        cursor_other_date,  # SELECT last_updated for other_source
        cursor_update,      # UPDATE chunks SET superseded_by
    ]

    similar_doc = Document(
        page_content='Architecture overview content',
        metadata={'source': other_source, 'filename': 'old-foo.md', 'similarity': 0.95},
    )
    mock_store.search_vector.return_value = [similar_doc]

    result = supersession_sweep(mock_store, source)

    assert len(result) == 1
    assert result[0] == (other_source, source)
    mock_store._conn.commit.assert_called_once()

    # Verify the UPDATE was called on the older source
    update_call = mock_store._conn.execute.call_args_list[3]
    assert 'UPDATE chunks SET superseded_by' in update_call[0][0]
    assert update_call[0][1][1] == other_source
