"""Todo 533: contextual embeddings, frontmatter stripping, meta guard, re-embed."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from core.embeddings import EMBED_PREFIX_VERSION, ONNXEmbeddings, build_embed_text
from core.indexer import chunk_file, embedding_meta, get_store, index_file
from core.reranker import Reranker
from core.store import EmbeddingConfigMismatch, Store

_spec = importlib.util.spec_from_file_location(
    'reembed_script', Path(__file__).resolve().parent.parent / 'scripts' / 'reembed.py')
reembed_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reembed_script)


class RecordingEmbed:
    """Records what text was embedded; vector content is irrelevant here."""
    def __init__(self):
        self.docs, self.queries = [], []

    def embed_documents(self, texts):
        self.docs.extend(texts)
        return [[0.1] * 384 for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [0.1] * 384


NOTE = '---\nid: 7\nstatus: pending\nproject: alpha\n---\n\n# Plan\n\n## Next Steps\n\ncheck on 11-02\n'


def _cfg(ws, **extra):
    return {'workspace': str(ws), 'exclude': [], 'chunk_size': 500, 'chunk_overlap': 100,
            'embedding_model': 'all-MiniLM-L6-v2', **extra}


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / 'ws'
    d.mkdir()
    return d


def test_frontmatter_stripped_from_chunk_text(ws):
    f = ws / 'n.md'
    f.write_text(NOTE)
    chunks = chunk_file(f, ws, _cfg(ws))
    joined = '\n'.join(c.page_content for c in chunks)
    assert 'status: pending' not in joined and '---' not in joined
    assert 'check on 11-02' in joined
    assert chunks[0].metadata['project'] == 'alpha'  # still parsed into metadata


def test_frontmatter_stripped_in_headerless_fallback(ws):
    f = ws / 'n.md'
    f.write_text('---\nid: 7\nstatus: pending\n---\n\njust a plain paragraph\n')
    chunks = chunk_file(f, ws, _cfg(ws))
    assert [c.page_content for c in chunks] == ['just a plain paragraph']


def test_frontmatter_only_file_yields_no_chunks(ws):
    f = ws / 'n.md'
    f.write_text('---\nid: 7\n---\n')
    assert chunk_file(f, ws, _cfg(ws)) == []


def test_prefix_embedded_but_content_stored_unchanged(ws, tmp_path):
    f = ws / 'n.md'
    f.write_text(NOTE)
    emb = RecordingEmbed()
    store = Store(str(tmp_path / 'r.db'), embed_fn=emb)
    index_file(str(f), _cfg(ws, embed_prefix=True), emb, store)

    assert all(t.startswith('n.md | ') and '| project: alpha\n' in t for t in emb.docs)
    stored = [r[0] for r in store._conn.execute('SELECT content FROM chunks')]
    assert stored and all(not s.startswith('n.md |') for s in stored)


def test_no_prefix_by_default(ws, tmp_path):
    f = ws / 'n.md'
    f.write_text(NOTE)
    emb = RecordingEmbed()
    index_file(str(f), _cfg(ws), emb, Store(str(tmp_path / 'r.db'), embed_fn=emb))
    assert not any(t.startswith('n.md |') for t in emb.docs)


def test_build_embed_text_shape():
    assert build_embed_text('body', 'a.md', 'H1 > H2', None) == 'a.md | H1 > H2 | project: \nbody'


def test_reranker_text_uses_prefix_only_when_contextual():
    doc = Document(page_content='body', metadata={'filename': 'a.md', 'headers': 'H', 'project': 'p'})
    plain = Reranker.__new__(Reranker)
    plain._contextual = False
    ctx = Reranker.__new__(Reranker)
    ctx._contextual = True
    assert plain._text(doc) == 'body'
    assert ctx._text(doc) == 'a.md | H | project: p\nbody'


def test_query_prefix_applied(monkeypatch):
    seen = []
    fake_model = SimpleNamespace(
        query_embed=lambda texts: (seen.extend(texts) or [SimpleNamespace(tolist=lambda: [0.0])]),
        passage_embed=lambda texts: [SimpleNamespace(tolist=lambda: [0.0]) for _ in texts],
    )
    monkeypatch.setattr('core.embeddings.TextEmbedding', lambda model_name: fake_model)
    emb = ONNXEmbeddings('x', query_prefix='Represent: ')
    emb.embed_query('hello')
    assert seen == ['Represent: hello']


def test_embedding_meta_records_then_rejects_mismatch(tmp_path):
    store = Store(str(tmp_path / 'r.db'))
    store.ensure_embedding_meta(embedding_meta({'embedding_model': 'all-MiniLM-L6-v2'}))
    assert store.get_meta('embedding_model') == 'sentence-transformers/all-MiniLM-L6-v2'
    assert store.get_meta('embed_prefix_version') == '0'
    # same config again is fine
    store.ensure_embedding_meta(embedding_meta({'embedding_model': 'all-MiniLM-L6-v2'}))
    with pytest.raises(EmbeddingConfigMismatch, match='reembed'):
        store.ensure_embedding_meta(embedding_meta({'embedding_model': 'BAAI/bge-small-en-v1.5'}))
    with pytest.raises(EmbeddingConfigMismatch):
        store.ensure_embedding_meta(embedding_meta(
            {'embedding_model': 'all-MiniLM-L6-v2', 'embed_prefix': True}))
    assert embedding_meta({'embedding_model': 'x', 'embed_prefix': True})['embed_prefix_version'] == EMBED_PREFIX_VERSION


def test_reembed_rechunks_files_carries_other_sources_and_drops_missing(ws, tmp_path):
    keep = ws / 'keep.md'
    keep.write_text(NOTE)
    gone = ws / 'gone.md'
    gone.write_text(NOTE)
    old_cfg = _cfg(ws)
    old_path = str(tmp_path / 'old.db')
    old = Store(old_path, embed_fn=RecordingEmbed())
    index_file(str(keep), old_cfg, RecordingEmbed(), old)
    index_file(str(gone), old_cfg, RecordingEmbed(), old)
    old.upsert_file('news/2026/x.md', [Document(page_content='headline text', metadata={
        'filename': 'x.md', 'folder': 'news', 'headers': 'news/2026/x.md', 'last_updated': '2026-10-01'})])
    # a DB-only supersession marker (no frontmatter equivalent)
    old._conn.execute("UPDATE chunks SET superseded_by = 'newer.md' WHERE source = ?", (str(keep),))
    old._conn.commit()
    os.remove(gone)

    new_cfg = _cfg(ws, embed_prefix=True)
    emb = RecordingEmbed()
    new_path = str(tmp_path / 'new.db')
    counts = reembed_script.reembed(old_path, new_path, new_cfg, emb, log=lambda *_: None)

    assert counts == {'rechunked': 1, 'carried': 1, 'dropped': 1, 'failed': 0}
    new = Store(new_path)
    assert set(new.list_sources()) == {str(keep), 'news/2026/x.md'}
    assert new.get_meta('embed_prefix_version') == EMBED_PREFIX_VERSION
    assert new._conn.execute('SELECT superseded_by FROM chunks WHERE source = ?', (str(keep),)).fetchone()[0] == 'newer.md'
    assert any(t.startswith('x.md | news/2026/x.md | project: \nheadline') for t in emb.docs)
    assert 'status: pending' not in ' '.join(r[0] for r in new._conn.execute('SELECT content FROM chunks'))
    # old DB untouched
    assert set(Store(old_path).list_sources()) == {str(keep), str(gone), 'news/2026/x.md'}


def test_reembed_refuses_to_overwrite_existing_db(ws, tmp_path):
    existing = tmp_path / 'new.db'
    existing.write_text('x')
    with pytest.raises(FileExistsError):
        reembed_script.reembed(str(tmp_path / 'old.db'), str(existing), _cfg(ws), RecordingEmbed())
