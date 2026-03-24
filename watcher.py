import os
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from indexer import load_config, get_embeddings, get_store, index_file, chunk_file
from search import get_store as search_get_store


class MarkdownHandler(FileSystemEventHandler):
    def __init__(self, cfg, embeddings, store):
        self.cfg = cfg
        self.embeddings = embeddings
        self.store = store
        self.workspace = Path(cfg['workspace'])
        self.exclude = set(cfg.get('exclude', []))

    def is_excluded(self, path):
        try:
            parts = Path(path).relative_to(self.workspace).parts
            return any(part in self.exclude for part in parts)
        except ValueError:
            return True

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] created: {event.src_path}', flush=True)
        try:
            index_file(event.src_path, self.cfg, self.embeddings, self.store)
        except Exception as e:
            print(f'[watcher] error indexing {event.src_path}: {e}', flush=True)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] modified: {event.src_path}', flush=True)
        try:
            index_file(event.src_path, self.cfg, self.embeddings, self.store)
        except Exception as e:
            print(f'[watcher] error indexing {event.src_path}: {e}', flush=True)

    def on_deleted(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        try:
            n = self.store.delete_file(str(event.src_path))
            print(f'[watcher] deleted: {event.src_path} ({n} chunks removed)', flush=True)
        except Exception as e:
            print(f'[watcher] error deleting {event.src_path}: {e}', flush=True)


def start_watcher():
    cfg = load_config()
    print('[watcher] loading embeddings...', flush=True)
    embeddings = get_embeddings(cfg)
    store = search_get_store()

    observer = Observer()

    # Watch primary workspace
    ws = cfg['workspace']
    handler = MarkdownHandler(cfg, embeddings, store)
    observer.schedule(handler, ws, recursive=True)
    print(f'[watcher] watching {ws}', flush=True)

    # Watch additional directories (e.g. Obsidian vault) if configured
    extra_dirs = cfg.get('watch_extra', [])
    for extra in extra_dirs:
        if Path(extra).exists():
            extra_cfg = {**cfg, 'workspace': extra, 'exclude': ['.trash', 'trash']}
            extra_handler = MarkdownHandler(extra_cfg, embeddings, store)
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
