"""Todo 530: watcher move/rename handling and stale-chunk reconciliation."""
import os
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from core.indexer import index_file, reconcile
from core.store import Store
from infra.watcher import MarkdownHandler


class FakeEmbed:
    def embed_documents(self, texts):
        return [[0.1] * 384 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 384


class ImmediateQueue:
    """Runs submitted work synchronously."""
    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / 'Claude'
    obs = tmp_path / 'Obsidian'
    for d in (ws / 'inbox', ws / 'trash', obs / 'Notes'):
        d.mkdir(parents=True)
    cfg = {
        'workspace': str(ws), 'exclude': ['trash', 'tmp'],
        'chunk_size': 500, 'chunk_overlap': 100,
        'watch_extra': [str(obs)],
    }
    store = Store(str(tmp_path / 'rag.db'), embed_fn=FakeEmbed())
    handler = MarkdownHandler(cfg, FakeEmbed(), store, ImmediateQueue())
    return SimpleNamespace(ws=ws, obs=obs, cfg=cfg, store=store, handler=handler)


def _write(path, text='# Title\n\nsome body text\n'):
    path.write_text(text, encoding='utf-8')
    return str(path)


def _sources(store):
    return set(store.list_sources())


def _move(handler, src, dest):
    os.rename(src, dest)
    handler.on_moved(SimpleNamespace(is_directory=False, src_path=str(src), dest_path=str(dest)))


def _index(env, path):
    # Mirror the watcher: extra roots are indexed with workspace=<that root>.
    cfg = {**env.cfg, 'workspace': str(env.obs)} if str(path).startswith(str(env.obs)) else env.cfg
    index_file(path, cfg, FakeEmbed(), env.store)


def test_move_within_workspace_reindexes_new_path(env):
    old = _write(env.ws / 'inbox' / 'a.md')
    _index(env, old)
    new = env.ws / 'inbox' / 'b.md'
    _move(env.handler, old, new)
    assert _sources(env.store) == {str(new)}


def test_move_into_trash_removes_chunks(env):
    old = _write(env.ws / 'inbox' / 'a.md')
    _index(env, old)
    _move(env.handler, old, env.ws / 'trash' / 'a.md')
    assert _sources(env.store) == set()


def test_atomic_write_rename_keeps_target_indexed(env):
    target = env.ws / 'inbox' / 'a.md'
    _index(env, _write(target))
    tmp = _write(env.ws / 'inbox' / '.tmp_1_a.md', '# Title\n\nnew body\n')
    _move(env.handler, tmp, target)
    assert _sources(env.store) == {str(target)}
    assert 'new body' in env.store._conn.execute('SELECT content FROM chunks').fetchone()[0]


def test_move_cancels_pending_debounce(env):
    old = _write(env.ws / 'inbox' / 'a.md')
    timer = SimpleNamespace(cancelled=False)
    timer.cancel = lambda: setattr(timer, 'cancelled', True)
    env.handler._debounce_timers[old] = timer
    _move(env.handler, old, env.ws / 'inbox' / 'b.md')
    assert timer.cancelled and old not in env.handler._debounce_timers


def test_delete_cancels_pending_debounce(env):
    path = _write(env.ws / 'inbox' / 'a.md')
    timer = SimpleNamespace(cancelled=False)
    timer.cancel = lambda: setattr(timer, 'cancelled', True)
    env.handler._debounce_timers[path] = timer
    env.handler.on_deleted(SimpleNamespace(is_directory=False, src_path=path))
    assert timer.cancelled


def test_emptied_file_drops_old_chunks(env):
    path = env.ws / 'inbox' / 'a.md'
    _index(env, _write(path))
    assert _sources(env.store) == {str(path)}
    _write(path, '')
    _index(env, str(path))
    assert _sources(env.store) == set()


def test_reconcile_removes_missing_and_excluded_only(env):
    keep = _write(env.ws / 'inbox' / 'keep.md')
    gone = _write(env.ws / 'inbox' / 'gone.md')
    trashed = _write(env.ws / 'trash' / 'old.md')
    obs_gone = _write(env.obs / 'Notes' / 'renamed.md')
    for p in (keep, gone, trashed, obs_gone):
        _index(env, p)
    os.remove(gone)
    os.remove(obs_gone)

    assert reconcile(env.store, env.cfg) == 3
    assert _sources(env.store) == {keep}


def test_reconcile_skips_non_file_sources(env):
    docs = [Document(page_content='x', metadata={'filename': 'n.md'})]
    env.store.upsert_file('news/2026/foo.md', docs)
    env.store.upsert_file('paperless:123', docs)
    env.store.upsert_file('/elsewhere/not-watched.md', docs)

    assert reconcile(env.store, env.cfg) == 0
    assert _sources(env.store) == {'news/2026/foo.md', 'paperless:123', '/elsewhere/not-watched.md'}


def test_reconcile_ignores_sibling_dir_with_shared_prefix(env):
    sibling = env.ws.parent / 'Claude-other'
    sibling.mkdir()
    path = _write(sibling / 'x.md')
    env.store.upsert_file(path, [Document(page_content='x', metadata={'filename': 'x.md'})])
    os.remove(path)

    assert reconcile(env.store, env.cfg) == 0
