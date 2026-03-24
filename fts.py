"""Persistent BM25 search via SQLite FTS5.

Replaces the in-memory rank-bm25 approach that loaded all 55k documents
from ChromaDB on every chain rebuild. FTS5 keeps the index on disk,
supports incremental INSERT/DELETE, and uses ~5-15MB RSS vs ~400-800MB.

Usage:
    from fts import FTSIndex
    fts = FTSIndex('/opt/rag/fts.db')
    fts.upsert_chunks('/mnt/Claude/todos/001-foo.md', chunks)
    results = fts.search('kanban board URL', k=6)
"""

import sqlite3
from pathlib import Path
from langchain_core.documents import Document


class FTSIndex:
    """SQLite FTS5-backed keyword search index."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                filename TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT 'root',
                content TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
        ''')
        # Rebuild FTS content table to sync with chunks (idempotent)
        self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self._conn.commit()

    def upsert_chunks(self, source: str, chunks: list[Document]):
        """Replace all chunks for a given source file."""
        cur = self._conn.cursor()
        # Delete old chunks for this source
        cur.execute('DELETE FROM chunks WHERE source = ?', (source,))
        # Insert new chunks
        for chunk in chunks:
            meta = chunk.metadata
            cur.execute(
                'INSERT INTO chunks (source, filename, folder, content) VALUES (?, ?, ?, ?)',
                (source, meta.get('filename', ''), meta.get('folder', 'root'), chunk.page_content),
            )
        self._conn.commit()

    def delete_file(self, source: str) -> int:
        """Remove all chunks for a source file. Returns count deleted."""
        cur = self._conn.cursor()
        cur.execute('SELECT COUNT(*) FROM chunks WHERE source = ?', (source,))
        count = cur.fetchone()[0]
        cur.execute('DELETE FROM chunks WHERE source = ?', (source,))
        self._conn.commit()
        return count

    def search(self, query: str, k: int = 6) -> list[Document]:
        """BM25-ranked keyword search. Returns top-k Documents."""
        # FTS5 match syntax: double-quote the query to handle special chars
        # Use bm25() for ranking (lower = more relevant in FTS5)
        cur = self._conn.execute('''
            SELECT c.content, c.source, c.filename, c.folder, chunks_fts.rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY chunks_fts.rank
            LIMIT ?
        ''', (self._fts_query(query), k))
        results = []
        for content, source, filename, folder, rank in cur.fetchall():
            doc = Document(
                page_content=content,
                metadata={'source': source, 'filename': filename, 'folder': folder},
            )
            results.append(doc)
        return results

    def count(self) -> int:
        cur = self._conn.execute('SELECT COUNT(*) FROM chunks')
        return cur.fetchone()[0]

    def rebuild_from_chroma(self, db):
        """One-time bulk load from an existing ChromaDB collection."""
        print('[fts] bulk loading from ChromaDB...', flush=True)
        page_size = 5000
        offset = 0
        total = 0
        while True:
            batch = db._collection.get(
                include=['documents', 'metadatas'], limit=page_size, offset=offset
            )
            if not batch['documents']:
                break
            cur = self._conn.cursor()
            for text, meta in zip(batch['documents'], batch['metadatas']):
                cur.execute(
                    'INSERT INTO chunks (source, filename, folder, content) VALUES (?, ?, ?, ?)',
                    (meta.get('source', ''), meta.get('filename', ''), meta.get('folder', 'root'), text),
                )
            self._conn.commit()
            total += len(batch['documents'])
            if len(batch['documents']) < page_size:
                break
            offset += page_size
        print(f'[fts] loaded {total} chunks into FTS5', flush=True)

    @staticmethod
    def _fts_query(query: str) -> str:
        """Convert a natural language query into FTS5 match syntax.
        Escapes special characters and joins terms with implicit AND.
        """
        # Strip FTS5 operators and special chars, keep alphanumeric + dots (for IPs)
        import re
        tokens = re.findall(r'[\w.]+', query)
        if not tokens:
            return '""'
        # Quote each token to handle dots/special chars, join with implicit AND
        return ' '.join(f'"{t}"' for t in tokens)

    def close(self):
        self._conn.close()
