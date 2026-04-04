import os
import re
import shutil
import time
import queue
import threading
from datetime import date
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from indexer import load_config, get_embeddings, get_store, index_file, chunk_file
from search import get_store as search_get_store

# Frontmatter fields injected into new notes under Notes/
_FRONTMATTER_DEFAULTS = {
    'date_created': None,   # filled with today's date at inject time
    'reviewed': 'unreviewed',
    'tags': '[]',
}

NOTES_SUBDIR = 'Notes'
ARCHIVE_SUBDIR = 'Notes/Archive'


def _parse_frontmatter(text):
    """Return (fields_dict, body_after_closing_fence) or (None, text) if no frontmatter."""
    if not text.startswith('---'):
        return None, text
    end = text.find('---', 3)
    if end == -1:
        return None, text
    raw = text[3:end]
    fields = {}
    for line in raw.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            fields[k.strip()] = v.strip()
    body = text[end + 3:]
    return fields, body


def _serialize_frontmatter(fields, body):
    """Rebuild a file's text from a fields dict and remaining body."""
    lines = ['---']
    for k, v in fields.items():
        lines.append(f'{k}: {v}')
    lines.append('---')
    return '\n'.join(lines) + body


def inject_frontmatter(path):
    """Inject missing frontmatter fields into a note. No-op if all present."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except Exception as e:
        print(f'[watcher] inject_frontmatter: could not read {path}: {e}', flush=True)
        return

    fields, body = _parse_frontmatter(text)
    if fields is None:
        fields = {}

    today = date.today().isoformat()
    defaults = {
        'date_created': today,
        'reviewed': 'unreviewed',
        'tags': '[]',
    }

    changed = False
    for k, v in defaults.items():
        if k not in fields:
            fields[k] = v
            changed = True

    if not changed:
        return

    new_text = _serialize_frontmatter(fields, body)
    # Write via a temp file in the same directory, then atomically replace.
    # rename() only requires write permission on the parent directory, so this
    # works even when the original file is owned by a different uid (e.g. nobody/65534
    # from Obsidian sync into an unprivileged LXC container).
    p = Path(path)
    tmp = p.parent / f'.tmp_{os.getpid()}_{p.name}'
    try:
        tmp.write_text(new_text, encoding='utf-8')
        os.replace(str(tmp), path)
        print(f'[watcher] injected frontmatter: {path}', flush=True)
    except Exception as e:
        print(f'[watcher] inject_frontmatter: could not write {path}: {e}', flush=True)
        tmp.unlink(missing_ok=True)


def is_in_notes_root(path, workspace):
    """Return True if path is directly inside Notes/ (not in a subdirectory like Notes/Archive/)."""
    try:
        rel = Path(path).relative_to(workspace)
        # Must be exactly Notes/<filename>.md — one level deep
        return rel.parts[0] == NOTES_SUBDIR and len(rel.parts) == 2
    except ValueError:
        return False


def archive_note(path, workspace, store):
    """Move a reviewed note from Notes/ to Notes/Archive/ and remove it from the index."""
    src = Path(path)
    archive_dir = Path(workspace) / ARCHIVE_SUBDIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / src.name

    # Avoid clobbering an existing archive file
    if dest.exists():
        stem = src.stem
        suffix = src.suffix
        dest = archive_dir / f'{stem}-{date.today().isoformat()}{suffix}'

    try:
        shutil.move(str(src), str(dest))
        print(f'[watcher] archived: {src.name} → {ARCHIVE_SUBDIR}/', flush=True)
    except Exception as e:
        print(f'[watcher] archive failed for {path}: {e}', flush=True)
        return

    try:
        n = store.delete_file(str(src))
        print(f'[watcher] removed from index: {src.name} ({n} chunks)', flush=True)
    except Exception as e:
        print(f'[watcher] index delete failed for {path}: {e}', flush=True)


def get_reviewed_value(path):
    """Return the reviewed state from frontmatter.

    Checks 'reviewed' first, then 'status' as a fallback so that daily notes
    (which use 'status: unreviewed' instead of 'reviewed: unreviewed') are also
    handled correctly.  Returns None if neither field is present.
    """
    try:
        text = Path(path).read_text(encoding='utf-8')
    except Exception:
        return None
    fields, _ = _parse_frontmatter(text)
    if fields is None:
        return None
    return fields.get('reviewed') or fields.get('status')


def startup_scan(cfg, store, index_queue, handler):
    """Process all existing .md files in Notes/ root at startup.

    Covers three cases the event-driven watcher misses:
      - Files that existed before the watcher started (no on_created fired)
      - Files that were marked reviewed while the service was down (on_modified missed)
      - Files that failed frontmatter injection on a previous run (e.g. permission error)
    """
    workspace = Path(cfg['workspace'])
    notes_dir = workspace / NOTES_SUBDIR
    if not notes_dir.exists():
        return

    count = 0
    for path in sorted(notes_dir.glob('*.md')):
        path_str = str(path)
        if handler.is_excluded(path_str):
            continue
        count += 1
        reviewed = get_reviewed_value(path_str)
        if reviewed is not None and reviewed != 'unreviewed':
            archive_note(path_str, str(workspace), store)
        else:
            inject_frontmatter(path_str)
            index_queue.submit(handler._do_index, path_str)

    print(f'[watcher] startup scan: {count} file(s) in {notes_dir}', flush=True)


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
        self._debounce_timers = {}  # path -> threading.Timer

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
        # Inject missing frontmatter for notes created directly in Notes/
        if is_in_notes_root(event.src_path, self.workspace):
            inject_frontmatter(event.src_path)
        self._queue.submit(self._do_index, event.src_path)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        if self.is_excluded(event.src_path):
            return
        path = event.src_path
        # Debounce: cancel any pending re-index for this file and restart the timer.
        # This means a file that is saved repeatedly (e.g. a note being edited)
        # only triggers one re-index, 8 seconds after the last save.
        existing = self._debounce_timers.pop(path, None)
        if existing:
            existing.cancel()
        t = threading.Timer(8.0, self._enqueue_modified, args=(path,))
        self._debounce_timers[path] = t
        t.start()

    def _enqueue_modified(self, path):
        self._debounce_timers.pop(path, None)
        # If a Notes/ file has been marked reviewed, archive it instead of re-indexing
        if is_in_notes_root(path, self.workspace):
            reviewed = get_reviewed_value(path)
            if reviewed is not None and reviewed != 'unreviewed':
                archive_note(path, str(self.workspace), self.store)
                return
        print(f'[watcher] modified: {path}', flush=True)
        self._queue.submit(self._do_index, path)

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
    startup_scan(cfg, store, index_queue, handler)

    # Watch additional directories (e.g. Obsidian vault) if configured
    extra_dirs = cfg.get('watch_extra', [])
    for extra in extra_dirs:
        if Path(extra).exists():
            extra_cfg = {**cfg, 'workspace': extra, 'exclude': ['.trash', 'trash']}
            extra_handler = MarkdownHandler(extra_cfg, embeddings, store, index_queue)
            observer.schedule(extra_handler, extra, recursive=True)
            print(f'[watcher] watching {extra}', flush=True)
            startup_scan(extra_cfg, store, index_queue, extra_handler)
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
