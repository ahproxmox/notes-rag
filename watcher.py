import os
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from indexer import load_config, get_embeddings, get_db, index_file, delete_file_chunks, chunk_file
from search import get_fts


class MarkdownHandler(FileSystemEventHandler):
    def __init__(self, cfg, embeddings, db):
        self.cfg = cfg
        self.embeddings = embeddings
        self.db = db
        self.workspace = Path(cfg['workspace'])
        self.exclude = set(cfg.get('exclude', []))

    def is_excluded(self, path):
        try:
            parts = Path(path).relative_to(self.workspace).parts
            return any(part in self.exclude for part in parts)
        except ValueError:
            return True

    def _index_and_sync_fts(self, src_path):
        """Index into ChromaDB and sync FTS5 in one step."""
        index_file(src_path, self.cfg, self.embeddings, self.db)
        # Sync FTS5 with the same chunks
        try:
            chunks = chunk_file(Path(src_path), self.workspace, self.cfg)
            get_fts().upsert_chunks(str(src_path), chunks)
        except Exception as e:
            print(f'[watcher] FTS sync error {src_path}: {e}', flush=True)

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] created: {event.src_path}', flush=True)
        try:
            self._index_and_sync_fts(event.src_path)
        except Exception as e:
            print(f'[watcher] error indexing {event.src_path}: {e}', flush=True)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        print(f'[watcher] modified: {event.src_path}', flush=True)
        try:
            self._index_and_sync_fts(event.src_path)
        except Exception as e:
            print(f'[watcher] error indexing {event.src_path}: {e}', flush=True)

    def on_deleted(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        try:
            n = delete_file_chunks(self.db, event.src_path)
            fts_n = get_fts().delete_file(str(event.src_path))
            print(f'[watcher] deleted: {event.src_path} ({n} chroma + {fts_n} fts chunks removed)', flush=True)
        except Exception as e:
            print(f'[watcher] error deleting {event.src_path}: {e}', flush=True)


def start_watcher():
    cfg = load_config()
    print('[watcher] loading embeddings...', flush=True)
    embeddings = get_embeddings(cfg)
    db = get_db(cfg, embeddings)

    observer = Observer()

    # Watch primary workspace
    ws = cfg['workspace']
    handler = MarkdownHandler(cfg, embeddings, db)
    observer.schedule(handler, ws, recursive=True)
    print(f'[watcher] watching {ws}', flush=True)

    # Watch additional directories (e.g. Obsidian vault) if configured
    extra_dirs = cfg.get('watch_extra', [])
    for extra in extra_dirs:
        if Path(extra).exists():
            extra_cfg = {**cfg, 'workspace': extra, 'exclude': ['.trash', 'trash']}
            extra_handler = MarkdownHandler(extra_cfg, embeddings, db)
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
