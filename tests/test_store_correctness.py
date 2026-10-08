"""Todo 531: thread-safe store, query-time decay, fail-loud indexing."""
import threading
from datetime import date, timedelta

import pytest
from langchain_core.documents import Document

import core.search as search_mod
from core.health import IndexHealth, health
from core.indexer import chunk_file, get_store, index_file
from core.store import Store


class FakeEmbed:
    def embed_documents(self, texts):
        return [[0.1] * 384 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 384


def _doc(text, **meta):
    meta.setdefault('filename', 'f.md')
    return Document(page_content=text, metadata=meta)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / 'rag.db'), embed_fn=FakeEmbed())


def test_each_thread_gets_its_own_connection(store):
    main_conn = store._conn
    seen = []
    t = threading.Thread(target=lambda: seen.append(store._conn))
    t.start()
    t.join()
    assert seen[0] is not main_conn
    assert store._conn is main_conn


def test_concurrent_search_never_sees_half_replaced_file(store):
    src = '/w/a.md'
    store.upsert_file(src, [_doc('zebra alpha'), _doc('zebra beta')])
    stop = threading.Event()
    errors, empties = [], []

    def reader():
        while not stop.is_set():
            try:
                if not store.search_bm25('zebra', k=10):
                    empties.append(1)
                store.search_vector('zebra', k=10)
            except Exception as e:  # noqa: BLE001 - the test asserts none occur
                errors.append(e)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(60):
            store.upsert_file(src, [_doc(f'zebra gamma {i}'), _doc(f'zebra delta {i}')])
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert not errors, errors
    assert not empties, f'{len(empties)} searches saw zero results mid-replace'


def test_failed_upsert_rolls_back_and_keeps_old_chunks(store):
    src = '/w/a.md'
    store.upsert_file(src, [_doc('keepme')])
    bad = _doc('boom')
    bad.metadata['confidence'] = object()  # cannot be bound by sqlite3
    with pytest.raises(Exception):
        store.upsert_file(src, [_doc('new'), bad])
    assert [d.page_content for d in store.search_bm25('keepme')] == ['keepme']
    assert not store.search_bm25('new')
    store.upsert_file(src, [_doc('after')])  # connection not left mid-transaction


def _retrieve_with(monkeypatch, store, docs):
    monkeypatch.setattr(search_mod, 'get_store', lambda: store)
    store.upsert_file('/w/a.md', docs)
    return search_mod._retrieve('zebra', k=10)


def test_decay_computed_at_query_time_from_last_updated(monkeypatch, store):
    old = (date.today() - timedelta(days=365)).isoformat()
    # Stored decay_factor says "fresh" (stale from index time); last_updated says old.
    docs = _retrieve_with(monkeypatch, store, [
        _doc('zebra', last_updated=old, decay_factor=1.0),
    ])
    assert docs[0].metadata['lifecycle_score'] < 0.5


def test_decay_falls_back_to_stored_factor_without_date(monkeypatch, store):
    docs = _retrieve_with(monkeypatch, store, [_doc('zebra', decay_factor=0.5)])
    assert docs[0].metadata['lifecycle_score'] == 0.5


def test_lifecycle_applied_before_top_k_cut(monkeypatch, store):
    old = (date.today() - timedelta(days=720)).isoformat()
    monkeypatch.setattr(search_mod, 'get_store', lambda: store)
    # Many old chunks match "zebra" strongly; one fresh chunk matches less strongly.
    store.upsert_file('/w/old.md', [_doc(f'zebra zebra zebra {i}', last_updated=old) for i in range(5)])
    store.upsert_file('/w/new.md', [_doc('zebra', filename='new.md', last_updated=date.today().isoformat())])
    top = search_mod._retrieve('zebra', k=2)
    assert any(d.metadata['filename'] == 'new.md' for d in top)


def test_created_frontmatter_used_for_last_updated(tmp_path):
    ws = tmp_path / 'ws'
    (ws / 'todos').mkdir(parents=True)
    f = ws / 'todos' / '001-x.md'
    f.write_text('---\nid: 1\ncreated: 2025-01-02\n---\n\n# T\n\nbody\n')
    chunks = chunk_file(f, ws, {'chunk_size': 500, 'chunk_overlap': 100})
    assert chunks[0].metadata['last_updated'] == '2025-01-02'


def test_index_failure_is_recorded_and_raised(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    cfg = {'workspace': str(ws), 'chunk_size': 500, 'chunk_overlap': 100}
    before = health.snapshot()['failures']
    with pytest.raises(Exception):
        index_file(str(ws / 'missing.md'), cfg, FakeEmbed(), Store(str(tmp_path / 'r.db'), FakeEmbed()))
    snap = health.snapshot()
    assert snap['failures'] == before + 1
    assert snap['degraded'] is True
    assert snap['recent_failures'][-1]['path'].endswith('missing.md')


def test_health_keeps_only_recent_failures():
    h = IndexHealth()
    for i in range(50):
        h.record_failure(f'/p/{i}', 'x')
    snap = h.snapshot()
    assert snap['failures'] == 50
    assert len(snap['recent_failures']) == 20
    assert snap['recent_failures'][-1]['path'] == '/p/49'


def test_pushgateway_payload_has_failure_counter(monkeypatch):
    h = IndexHealth()
    h.set_queue_depth_fn(lambda: 7)
    h.record_failure('/p', 'x')
    text = h._metrics_text()
    assert 'notes_rag_index_failures_total 1' in text
    assert 'notes_rag_index_queue_depth 7' in text


def test_get_store_prefers_db_path(tmp_path, monkeypatch):
    cfg = {'db_path': str(tmp_path / 'custom.db'), 'chroma_path': '/nonexistent/chroma'}
    s = get_store(cfg, FakeEmbed())
    assert s._db_path == str(tmp_path / 'custom.db')
    legacy = get_store({'chroma_path': str(tmp_path / 'chroma')}, FakeEmbed())
    assert legacy._db_path == str(tmp_path / 'rag.db')
