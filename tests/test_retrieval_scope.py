"""Todo 532: /retrieve, news scope + retention, filtered vector search."""
import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

os.environ.setdefault('OPENROUTER_API_KEY', 'test')

import core.search as search_mod
from core.store import NEWS_FOLDERS, Store


class AxisEmbed:
    """'far' texts embed on axis 1, everything else (incl. queries) on axis 0."""
    @staticmethod
    def _vec(text):
        v = [0.0] * 384
        v[1 if 'far' in text else 0] = 1.0
        return v

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _doc(text, **meta):
    meta.setdefault('filename', 'f.md')
    return Document(page_content=text, metadata=meta)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / 'rag.db'), embed_fn=AxisEmbed())


def test_filtered_vector_search_returns_min_k_available(store):
    # 40 near chunks in project "big" crowd out the 3 far chunks in "small".
    store.upsert_file('/w/big.md', [_doc(f'near {i}', project='big') for i in range(40)])
    store.upsert_file('/w/small.md', [_doc(f'far {i}', project='small') for i in range(3)])
    got = store.search_vector('near', k=5, project='small')
    assert len(got) == 3
    assert {d.metadata['project'] for d in got} == {'small'}


def test_filtered_vector_search_stops_when_nothing_matches(store):
    store.upsert_file('/w/big.md', [_doc(f'near {i}', project='big') for i in range(10)])
    assert store.search_vector('near', k=5, project='nope') == []


def _seed_scopes(store):
    store.upsert_file('/w/note.md', [_doc('zebra note', folder='todos')])
    store.upsert_file('news/2026/a.md', [_doc('zebra news', folder='news', filename='a.md')])
    store.upsert_file('filings/b.md', [_doc('zebra filing', folder='filings', filename='b.md')])


@pytest.mark.parametrize('method', ['search_bm25', 'search_vector'])
def test_scope_filters_news(store, method):
    _seed_scopes(store)
    search = getattr(store, method)
    folders = lambda scope: {d.metadata['folder'] for d in search('zebra', k=10, scope=scope)}
    assert folders('notes') == {'todos'}
    assert folders('news') == {'news', 'filings'}
    assert folders('all') == {'todos', 'news', 'filings'}
    assert folders(None) == {'todos', 'news', 'filings'}


def test_explicit_folder_overrides_scope(store):
    _seed_scopes(store)
    got = store.search_bm25('zebra', k=10, folder='news', scope='notes')
    assert {d.metadata['folder'] for d in got} == {'news'}


def test_invalid_scope_raises(store):
    with pytest.raises(ValueError):
        store.search_bm25('zebra', scope='bogus')


def test_prune_older_than_only_touches_given_folders_and_dated_rows(store):
    old = (date.today() - timedelta(days=200)).isoformat()
    new = date.today().isoformat()
    store.upsert_file('news/old.md', [_doc('x', folder='news', last_updated=old)])
    store.upsert_file('news/new.md', [_doc('x', folder='news', last_updated=new)])
    store.upsert_file('news/undated.md', [_doc('x', folder='news')])
    store.upsert_file('/w/old-note.md', [_doc('x', folder='todos', last_updated=old)])

    assert store.prune_older_than(NEWS_FOLDERS, days=90) == 1
    assert set(store.list_sources()) == {'news/new.md', 'news/undated.md', '/w/old-note.md'}


def test_default_scope_is_all_unless_env_set(monkeypatch):
    monkeypatch.delenv('RAG_SEARCH_DEFAULT_SCOPE', raising=False)
    assert search_mod._default_scope() == 'all'
    monkeypatch.setenv('RAG_SEARCH_DEFAULT_SCOPE', 'notes')
    assert search_mod._default_scope() == 'notes'


def test_retrieve_makes_no_llm_call_and_returns_path(monkeypatch, store):
    _seed_scopes(store)
    monkeypatch.setattr(search_mod, 'get_store', lambda: store)
    monkeypatch.setattr(search_mod, '_get_reranker', lambda: None)

    def boom(*a, **k):
        raise AssertionError('LLM must not be called by retrieve()')
    monkeypatch.setattr(search_mod, '_get_llm', boom)

    chunks = search_mod.retrieve('zebra', k=5)  # default scope = notes
    assert [c['folder'] for c in chunks] == ['todos']
    assert chunks[0]['path'] == '/w/note.md'
    assert {'content', 'source', 'path', 'headers', 'score', 'lifecycle_score'} <= chunks[0].keys()


def test_retrieve_endpoint_passes_params_through():
    from api import app
    client = TestClient(app)
    with patch('api.app.retrieve', return_value=[{'content': 'c'}]) as m:
        r = client.post('/retrieve', json={'query': 'q', 'k': 3, 'scope': 'news', 'rerank': False})
    assert r.status_code == 200
    assert r.json() == {'chunks': [{'content': 'c'}]}
    assert m.call_args.kwargs['scope'] == 'news' and m.call_args.kwargs['rerank'] is False
    assert m.call_args.kwargs['k'] == 3


def test_retrieve_endpoint_rejects_bad_scope():
    from api import app
    r = TestClient(app).post('/retrieve', json={'query': 'q', 'scope': 'bogus'})
    assert r.status_code == 422
