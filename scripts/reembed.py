#!/usr/bin/env python3
"""One-off: re-embed the whole index into a NEW database file (todo 533).

    python scripts/reembed.py --new /opt/rag/rag.db.new [--old /opt/rag/rag.db]

Uses the embedding model / prefix settings currently in indexer.yaml. The old DB
is opened read-only and never modified. Afterwards: run bench against the new DB,
then stop the service, `mv` it over rag.db, and restart. Don't run while you
expect the watcher to keep the old DB current — re-run (or reconcile) after swap.

  - Files that still exist under a watched root are re-chunked from disk (picks up
    frontmatter stripping, chunk_size, prefix) and any DB-only `superseded_by`
    marker is carried over.
  - Files that are gone are dropped (same effect as reconcile).
  - Non-file sources (news/…, paperless:…) have no file to re-chunk, so their
    stored chunks are carried over and only re-embedded.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.documents import Document  # noqa: E402

from core.embeddings import build_embed_text  # noqa: E402
from core.indexer import (  # noqa: E402
    chunk_file, embedding_meta, get_embeddings, load_config, watched_roots,
)
from core.store import Store  # noqa: E402

COLUMNS = ('content', 'filename', 'folder', 'headers', 'wing', 'room', 'project',
           'superseded_by', 'confidence', 'last_updated', 'decay_factor')


def _root_for(source, roots):
    for root, _ in roots:
        if source.startswith(os.path.join(root, '')):
            return root
    return None


def reembed(old_db, new_db, cfg, embeddings, log=print):
    """Re-embed `old_db` into a fresh `new_db`. Returns a dict of counts."""
    if os.path.exists(new_db):
        raise FileExistsError(f'{new_db} already exists; refusing to overwrite')

    old = sqlite3.connect(f'file:{old_db}?mode=ro', uri=True)
    new = Store(new_db, embed_fn=embeddings)
    new.ensure_embedding_meta(embedding_meta(cfg))
    new.set_meta('chunk_size', str(cfg['chunk_size']))
    roots = watched_roots(cfg)
    counts = {'rechunked': 0, 'carried': 0, 'dropped': 0, 'failed': 0}

    sources = [r[0] for r in old.execute('SELECT DISTINCT source FROM chunks ORDER BY source')]
    for i, source in enumerate(sources):
        rows = old.execute(
            f'SELECT {", ".join(COLUMNS)} FROM chunks WHERE source = ? ORDER BY id', (source,)
        ).fetchall()
        root = _root_for(source, roots)
        try:
            if root is not None:
                if not os.path.exists(source):
                    counts['dropped'] += 1
                    continue
                file_cfg = {**cfg, 'workspace': root}
                chunks = chunk_file(Path(source), Path(root), file_cfg)
                superseded = next((r[COLUMNS.index('superseded_by')] for r in rows
                                   if r[COLUMNS.index('superseded_by')]), None)
                for c in chunks:
                    if superseded and not c.metadata.get('superseded_by'):
                        c.metadata['superseded_by'] = superseded
                counts['rechunked'] += 1
            else:
                chunks = []
                for r in rows:
                    meta = dict(zip(COLUMNS, r))
                    content = meta.pop('content')
                    if cfg.get('embed_prefix'):
                        meta['embed_text'] = build_embed_text(
                            content, meta['filename'], meta['headers'], meta['project'])
                    chunks.append(Document(page_content=content, metadata=meta))
                counts['carried'] += 1
            if chunks:
                new.upsert_file(source, chunks)
        except Exception as e:  # noqa: BLE001 - keep going, but report loudly at the end
            counts['failed'] += 1
            log(f'[reembed] FAILED {source}: {e}')
        if (i + 1) % 200 == 0:
            log(f'[reembed] {i + 1}/{len(sources)} sources...')

    old.close()
    log(f'[reembed] done: {counts}; {new.count()} chunks in {new_db}')
    return counts


def main():
    cfg = load_config()
    default_old = cfg.get('db_path') or os.path.join(os.path.dirname(cfg['chroma_path']), 'rag.db')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--old', default=default_old)
    ap.add_argument('--new', required=True)
    args = ap.parse_args()
    counts = reembed(args.old, args.new, cfg, get_embeddings(cfg))
    sys.exit(1 if counts['failed'] else 0)


if __name__ == '__main__':
    main()
