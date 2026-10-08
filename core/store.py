"""Unified SQLite store: FTS5 keyword search + sqlite-vec vector search.

Replaces both ChromaDB and the separate FTS5 index (fts.py) with a single
SQLite database. Vectors and full-text are stored side-by-side, enabling
metadata filtering before ranking and simpler operational management.

Usage:
    from core.store import Store
    store = Store('/opt/rag/rag.db', embed_fn)
    store.upsert_file('/mnt/Claude/todos/001-foo.md', chunks)
    bm25_docs = store.search_bm25('kanban board', k=20)
    vec_docs = store.search_vector('kanban board', k=20)
"""

import struct
import sqlite3
import threading
from pathlib import Path
from langchain_core.documents import Document

import sqlite_vec


# Folders produced by POST /ingest from the trading-enrich pipeline.
NEWS_FOLDERS = ('news', 'filings')
SCOPES = ('notes', 'news', 'all')

# sqlite-vec rejects k above 4096.
VEC_MAX_K = 4096


def _scope_clause(scope: str | None, folder: str | None):
    """SQL fragment + params restricting chunks by scope.

    An explicit `folder` filter wins over scope.
    """
    if scope is None or scope == 'all' or folder:
        return None, []
    if scope not in SCOPES:
        raise ValueError(f'invalid scope {scope!r}; expected one of {SCOPES}')
    marks = ','.join('?' * len(NEWS_FOLDERS))
    if scope == 'news':
        return f'c.folder IN ({marks})', list(NEWS_FOLDERS)
    return f'c.folder NOT IN ({marks})', list(NEWS_FOLDERS)


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector for sqlite-vec."""
    return struct.pack(f'{len(vec)}f', *vec)


class Store:
    """Unified SQLite store with FTS5 + sqlite-vec."""

    def __init__(self, db_path: str, embed_fn=None, vec_dim: int = 384):
        self._db_path = db_path
        self._embed_fn = embed_fn
        self._vec_dim = vec_dim
        # One connection per thread: sqlite3 connections aren't safe for concurrent
        # use, and the watcher's index thread writes while API threads read.
        # WAL lets readers proceed during a write.
        self._local = threading.local()
        self._init_tables()

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute('PRAGMA journal_mode=WAL')
            self._local.conn = conn
        return conn

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
        # Wing/room/project/superseded_by/lifecycle columns — added via ALTER for
        # forward compatibility with existing databases.
        for col, default in (
            ('wing', 'TEXT'),
            ('room', 'TEXT'),
            ('project', 'TEXT'),
            ('superseded_by', 'TEXT'),
            ('confidence', 'REAL DEFAULT 1.0'),
            ('last_updated', 'TEXT'),
            ('decay_factor', 'REAL DEFAULT 1.0'),
        ):
            try:
                self._conn.execute(f'ALTER TABLE chunks ADD COLUMN {col} {default}')
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_wing ON chunks(wing)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_wing_room ON chunks(wing, room)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_chunks_superseded ON chunks(superseded_by)')
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

        conn = self._conn
        cur = conn.cursor()
        # Single write transaction so readers never see a half-replaced file.
        cur.execute('BEGIN IMMEDIATE')
        try:
            self._replace_file(cur, source, chunks, embeddings)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    def _replace_file(self, cur, source, chunks, embeddings):
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
                'INSERT INTO chunks (source, filename, folder, headers, content, wing, room, project, superseded_by, confidence, last_updated, decay_factor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (source, meta.get('filename', ''), meta.get('folder', 'root'),
                 meta.get('headers', ''), chunk.page_content,
                 meta.get('wing'), meta.get('room'), meta.get('project'),
                 meta.get('superseded_by'),
                 meta.get('confidence', 1.0),
                 meta.get('last_updated'),
                 meta.get('decay_factor', 1.0)),
            )
            chunk_id = cur.lastrowid
            # Prepend filename so filename terms are searchable via BM25.
            # The chunks table keeps the original page_content unchanged (used for LLM context).
            fts_text = f"{meta.get('filename', '')} {chunk.page_content}"
            cur.execute('INSERT INTO chunks_fts (rowid, content) VALUES (?, ?)', (chunk_id, fts_text))
            if embeddings and i < len(embeddings):
                cur.execute(
                    'INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)',
                    (chunk_id, _serialize_f32(embeddings[i])),
                )

    def delete_file(self, source: str) -> int:
        """Remove all chunks for a source file."""
        conn = self._conn
        cur = conn.cursor()
        cur.execute('BEGIN IMMEDIATE')
        try:
            old_ids = [r[0] for r in cur.execute('SELECT id FROM chunks WHERE source = ?', (source,)).fetchall()]
            if old_ids:
                placeholders = ','.join('?' * len(old_ids))
                cur.execute(f'DELETE FROM chunks_fts WHERE rowid IN ({placeholders})', old_ids)
                cur.execute(f'DELETE FROM chunks_vec WHERE chunk_id IN ({placeholders})', old_ids)
                cur.execute('DELETE FROM chunks WHERE source = ?', (source,))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return len(old_ids)

    def list_sources(self, prefixes: tuple[str, ...] | None = None) -> list[str]:
        """Distinct chunk sources, optionally limited to those starting with a prefix."""
        if not prefixes:
            rows = self._conn.execute('SELECT DISTINCT source FROM chunks').fetchall()
            return [r[0] for r in rows]
        sources = []
        for prefix in prefixes:
            # substr comparison instead of LIKE so '_' / '%' in paths aren't wildcards
            rows = self._conn.execute(
                'SELECT DISTINCT source FROM chunks WHERE substr(source, 1, ?) = ?',
                (len(prefix), prefix),
            ).fetchall()
            sources.extend(r[0] for r in rows)
        return sources

    def search_bm25(self, query: str, k: int = 20, folder: str | None = None,
                    wing: str | None = None, room: str | None = None,
                    project: str | None = None, include_superseded: bool = False,
                    scope: str | None = None) -> list[Document]:
        """BM25-ranked keyword search with optional folder/wing/room/project/scope filters."""
        fts_query = self._fts_query(query)
        where = ['chunks_fts MATCH ?']
        params: list = [fts_query]
        if folder:
            where.append('c.folder = ?')
            params.append(folder)
        if wing:
            where.append('c.wing = ?')
            params.append(wing)
        if room:
            where.append('c.room = ?')
            params.append(room)
        if project:
            where.append('c.project = ?')
            params.append(project)
        if not include_superseded:
            where.append("(c.superseded_by IS NULL OR c.superseded_by = '')")
        scope_sql, scope_params = _scope_clause(scope, folder)
        if scope_sql:
            where.append(scope_sql)
            params.extend(scope_params)
        sql = f'''
            SELECT c.content, c.source, c.filename, c.folder, c.headers, c.wing, c.room, c.project,
                   c.confidence, c.decay_factor, c.superseded_by, c.last_updated
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE {' AND '.join(where)}
            ORDER BY chunks_fts.rank
            LIMIT ?
        '''
        params.append(k)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            Document(
                page_content=r[0],
                metadata={'source': r[1], 'filename': r[2], 'folder': r[3],
                          'headers': r[4], 'wing': r[5], 'room': r[6], 'project': r[7],
                          'confidence': float(r[8]) if r[8] is not None else 1.0,
                          'decay_factor': float(r[9]) if r[9] is not None else 1.0,
                          'superseded_by': r[10], 'last_updated': r[11]},
            )
            for r in rows
        ]

    def search_vector(self, query: str, k: int = 20, folder: str | None = None,
                      wing: str | None = None, room: str | None = None,
                      project: str | None = None, include_superseded: bool = False,
                      scope: str | None = None) -> list[Document]:
        """Vector similarity search with optional folder/wing/room/project/scope filters.

        sqlite-vec's k param is pre-filter — applied before our metadata WHERE
        clauses. To still return k results after filtering we over-fetch, and if
        the filter leaves fewer than k we retry with a larger fetch until k are
        found, the whole index has been scanned, or VEC_MAX_K is reached.

        Returned Documents include `similarity` in metadata (1 - cosine distance).
        """
        if self._embed_fn is None:
            return []
        query_vec = self._embed_fn.embed_query(query)
        query_bytes = _serialize_f32(query_vec)

        filters: list[str] = []
        fparams: list = []
        if folder:
            filters.append('c.folder = ?')
            fparams.append(folder)
        if wing:
            filters.append('c.wing = ?')
            fparams.append(wing)
        if room:
            filters.append('c.room = ?')
            fparams.append(room)
        if project:
            filters.append('c.project = ?')
            fparams.append(project)
        if not include_superseded:
            filters.append("(c.superseded_by IS NULL OR c.superseded_by = '')")
        scope_sql, scope_params = _scope_clause(scope, folder)
        if scope_sql:
            filters.append(scope_sql)
            fparams.extend(scope_params)

        sql = f'''
            SELECT c.content, c.source, c.filename, c.folder, c.headers, c.wing, c.room, c.project,
                   c.confidence, c.decay_factor, c.superseded_by, c.last_updated, v.distance
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.chunk_id
            WHERE {' AND '.join(['v.embedding MATCH ?', 'k = ?'] + filters)}
            ORDER BY v.distance
        '''
        if not filters:
            rows = self._conn.execute(sql, [query_bytes, k]).fetchall()
        else:
            fetch_k = min(k * 3, VEC_MAX_K)
            total = None
            while True:
                rows = self._conn.execute(sql, [query_bytes, fetch_k, *fparams]).fetchall()
                if len(rows) >= k or fetch_k >= VEC_MAX_K:
                    break
                if total is None:
                    total = self._conn.execute('SELECT COUNT(*) FROM chunks_vec').fetchone()[0]
                if fetch_k >= total:
                    break  # scanned every vector; fewer than k matches exist
                fetch_k = min(fetch_k * 4, VEC_MAX_K)
            rows = rows[:k]
        return [
            Document(
                page_content=r[0],
                metadata={'source': r[1], 'filename': r[2], 'folder': r[3],
                          'headers': r[4], 'wing': r[5], 'room': r[6], 'project': r[7],
                          'confidence': float(r[8]) if r[8] is not None else 1.0,
                          'decay_factor': float(r[9]) if r[9] is not None else 1.0,
                          'superseded_by': r[10], 'last_updated': r[11],
                          'similarity': 1.0 - float(r[12])},
            )
            for r in rows
        ]

    def prune_older_than(self, folders: tuple[str, ...], days: int) -> int:
        """Delete every source in `folders` whose newest chunk is older than `days`.

        Sources with no last_updated are kept (age unknown). Returns files removed.
        """
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        marks = ','.join('?' * len(folders))
        rows = self._conn.execute(
            f'SELECT source FROM chunks WHERE folder IN ({marks}) '
            'GROUP BY source HAVING MAX(last_updated) IS NOT NULL AND MAX(last_updated) < ?',
            [*folders, cutoff],
        ).fetchall()
        for (source,) in rows:
            self.delete_file(source)
        return len(rows)

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
        """Close the calling thread's connection."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None
