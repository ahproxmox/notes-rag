import os
import time
import queue
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from indexer import load_config, get_embeddings, get_store, index_file, chunk_file
from search import get_store as search_get_store


class IndexQueue:
    """Serializes all indexing work onto a single background thread."""
    def __init__(self):
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, fn, *args):
        self._q.put((fn, args))

    def _worker(self):
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception as e:
                print(f'[indexer] unhandled error: {e}', flush=True)
            finally:
                self._q.task_done()


class MarkdownHandler(FileSystemEventHandler):
    def __init__(self, cfg, embeddings, store, index_queue):
        self.cfg = cfg
        self.embeddings = embeddings
        self.store = store
        self.workspace = Path(cfg['workspace'])
        self.exclude = set(cfg.get('exclude', []))
        self._queue = index_queue

    def is_excluded(self, path):
        try:
            parts = Path(path).relative_to(self.workspace).parts
            return any(part in self.exclude for part in parts)
        except ValueError:
            return True

    def _do_index(self, path):
        try:
            index_file(path, self.cfg, self.embeddings, self.store)
        except Exception as e:
            print(f'[watcher] error indexing {path}: {e}', flush=True)

    def _do_delete(self, path):
        try:
            n = self.store.delete_file(str(path))
            print(f'[watcher] deleted: {path} ({n} chunks removed)', flush=True)
        except Exception as e:
            print(f'[watcher] error deleting {path}: {e}', flush=True)

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] created: {event.src_path}', flush=True)
        self._queue.submit(self._do_index, event.src_path)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] modified: {event.src_path}', flush=True)
        self._queue.submit(self._do_index, event.src_path)

    def on_deleted(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        self._queue.submit(self._do_delete, event.src_path)


def start_watcher():
    cfg = load_config()
    print('[watcher] loading embeddings...', flush=True)
    embeddings = get_embeddings(cfg)
    store = search_get_store()

    index_queue = IndexQueue()
    observer = Observer()

    # Watch primary workspace
    ws = cfg['workspace']
    handler = MarkdownHandler(cfg, embeddings, store, index_queue)
    observer.schedule(handler, ws, recursive=True)
    print(f'[watcher] watching {ws}', flush=True)

    # Watch additional directories (e.g. Obsidian vault) if configured
    extra_dirs = cfg.get('watch_extra', [])
    for extra in extra_dirs:
        if Path(extra).exists():
            extra_cfg = {**cfg, 'workspace': extra, 'exclude': ['.trash', 'trash']}
            extra_handler = MarkdownHandler(extra_cfg, embeddings, store, index_queue)
            observer.schedule(extra_handler, extra, recursive=True)
            print(f'[watcher] watching {extra}', flush=True)
        else:
            print(f'[watcher] {extra} not found, skipping', flush=True)

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == '__main__':
    start_watcher()
