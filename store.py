"""Unified SQLite store: FTS5 keyword search + sqlite-vec vector search.

Replaces both ChromaDB and the separate FTS5 index (fts.py) with a single
SQLite database. Vectors and full-text are stored side-by-side, enabling
metadata filtering before ranking and simpler operational management.

Usage:
    from store import Store
    store = Store('/opt/rag/rag.db', embed_fn)
    store.upsert_file('/mnt/Claude/todos/001-foo.md', chunks)
    bm25_docs = store.search_bm25('kanban board', k=20)
    vec_docs = store.search_vector('kanban board', k=20)
"""

import struct
import sqlite3
from pathlib import Path
from langchain_core.documents import Document

import sqlite_vec


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector for sqlite-vec."""
    return struct.pack(f'{len(vec)}f', *vec)


class Store:
    """Unified SQLite store with FTS5 + sqlite-vec."""

    def __init__(self, db_path: str, embed_fn=None, vec_dim: int = 384):
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._vec_dim = vec_dim
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript(f'''
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                filename TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT 'root',
                headers TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
            CREATE INDEX IF NOT EXISTS idx_chunks_folder ON chunks(folder);
        ''')
        # FTS5 virtual table (content-sync with chunks)
        self._conn.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                content_rowid='id',
                tokenize='porter unicode61'
            )
        ''')
        # sqlite-vec virtual table
        self._conn.execute(f'''
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding float[{self._vec_dim}]
            )
        ''')
        self._conn.commit()

    def upsert_file(self, source: str, chunks: list[Document], embeddings: list[list[float]] | None = None):
        """Replace all chunks for a source file. Embeds if embeddings not provided."""
        if embeddings is None and self._embed_fn is not None:
            texts = [c.page_content for c in chunks]
            embeddings = self._embed_fn.embed_documents(texts)

        cur = self._conn.cursor()
        # Delete old data for this source (chunks, FTS, and vectors)
        old_ids = [r[0] for r in cur.execute('SELECT id FROM chunks WHERE source = ?', (source,)).fetchall()]
        if old_ids:
            placeholders = ','.join('?' * len(old_ids))
            cur.execute(f'DELETE FROM chunks_fts WHERE rowid IN ({placeholders})', old_ids)
            cur.execute(f'DELETE FROM chunks_vec WHERE chunk_id IN ({placeholders})', old_ids)
            cur.execute('DELETE FROM chunks WHERE source = ?', (source,))

        # Insert new chunks
        for i, chunk in enumerate(chunks):
            meta = chunk.metadata
            cur.execute(
                'INSERT INTO chunks (source, filename, folder, headers, content) VALUES (?, ?, ?, ?, ?)',
                (source, meta.get('filename', ''), meta.get('folder', 'root'),
                 meta.get('headers', ''), chunk.page_content),
            )
            chunk_id = cur.lastrowid
            cur.execute('INSERT INTO chunks_fts (rowid, content) VALUES (?, ?)', (chunk_id, chunk.page_content))
            if embeddings and i < len(embeddings):
                cur.execute(
                    'INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)',
                    (chunk_id, _serialize_f32(embeddings[i])),
                )
        self._conn.commit()

    def delete_file(self, source: str) -> int:
        """Remove all chunks for a source file."""
        cur = self._conn.cursor()
        old_ids = [r[0] for r in cur.execute('SELECT id FROM chunks WHERE source = ?', (source,)).fetchall()]
        if not old_ids:
            return 0
        placeholders = ','.join('?' * len(old_ids))
        cur.execute(f'DELETE FROM chunks_fts WHERE rowid IN ({placeholders})', old_ids)
        cur.execute(f'DELETE FROM chunks_vec WHERE chunk_id IN ({placeholders})', old_ids)
        cur.execute('DELETE FROM chunks WHERE source = ?', (source,))
        self._conn.commit()
        return len(old_ids)

    def search_bm25(self, query: str, k: int = 20, folder: str | None = None) -> list[Document]:
        """BM25-ranked keyword search with optional folder filter."""
        fts_query = self._fts_query(query)
        if folder:
            sql = '''
                SELECT c.content, c.source, c.filename, c.folder, c.headers
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ? AND c.folder = ?
                ORDER BY chunks_fts.rank
                LIMIT ?
            '''
            rows = self._conn.execute(sql, (fts_query, folder, k)).fetchall()
        else:
            sql = '''
                SELECT c.content, c.source, c.filename, c.folder, c.headers
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY chunks_fts.rank
                LIMIT ?
            '''
            rows = self._conn.execute(sql, (fts_query, k)).fetchall()
        return [
            Document(
                page_content=r[0],
                metadata={'source': r[1], 'filename': r[2], 'folder': r[3], 'headers': r[4]},
            )
            for r in rows
        ]

    def search_vector(self, query: str, k: int = 20, folder: str | None = None) -> list[Document]:
        """Vector similarity search with optional folder filter."""
        if self._embed_fn is None:
            return []
        query_vec = self._embed_fn.embed_query(query)
        query_bytes = _serialize_f32(query_vec)

        if folder:
            sql = '''
                SELECT c.content, c.source, c.filename, c.folder, c.headers, v.distance
                FROM chunks_vec v
                JOIN chunks c ON c.id = v.chunk_id
                WHERE v.embedding MATCH ? AND k = ? AND c.folder = ?
                ORDER BY v.distance
            '''
            rows = self._conn.execute(sql, (query_bytes, k * 3, folder)).fetchall()
            # sqlite-vec k param is pre-filter, so we need to trim after folder filter
            rows = rows[:k]
        else:
            sql = '''
                SELECT c.content, c.source, c.filename, c.folder, c.headers, v.distance
                FROM chunks_vec v
                JOIN chunks c ON c.id = v.chunk_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
            '''
            rows = self._conn.execute(sql, (query_bytes, k)).fetchall()
        return [
            Document(
                page_content=r[0],
                metadata={'source': r[1], 'filename': r[2], 'folder': r[3], 'headers': r[4]},
            )
            for r in rows
        ]

    def count(self) -> int:
        return self._conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]

    def rebuild_fts(self):
        """Rebuild FTS5 content index from chunks table."""
        self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self._conn.commit()

    @staticmethod
    def _fts_query(query: str) -> str:
        """Convert natural language query to FTS5 match syntax."""
        import re
        tokens = re.findall(r'[\w.]+', query)
        if not tokens:
            return '""'
        return ' '.join(f'"{t}"' for t in tokens)

    def close(self):
        self._conn.close()
