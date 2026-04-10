"""Lightweight entity tracker — structured registry of containers, services,
repos, people, and concepts that live alongside the RAG.

Purpose: answer "what is CT 122?" or "where does notes-rag run?" without an
LLM call, and give OpenClaw + Claude Code a shared source of truth that
doesn't drift with every session summary.

Design notes:
  - Separate SQLite DB (`entities.db`) to keep rag.db schema stable.
  - Flat table, no relation graph. Relations live inside `attrs` JSON as
    slug references (e.g. `{"runs_on": "ct-111"}`). Upgrade if we outgrow it.
  - FTS5 over name/summary/aliases for fuzzy lookup.
  - Upsert by slug — imports are idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class EntityStore:
    """SQLite-backed store for entities. One row per entity, keyed by slug."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS entities (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                slug     TEXT UNIQUE NOT NULL,
                type     TEXT NOT NULL,
                name     TEXT NOT NULL,
                wing     TEXT,
                summary  TEXT,
                attrs    TEXT NOT NULL DEFAULT '{}',
                aliases  TEXT NOT NULL DEFAULT '[]',
                created  TEXT NOT NULL,
                updated  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
            CREATE INDEX IF NOT EXISTS idx_entities_wing ON entities(wing);
            CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                slug, name, summary, aliases,
                content='entities', content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
                INSERT INTO entities_fts(rowid, slug, name, summary, aliases)
                VALUES (new.id, new.slug, new.name, new.summary, new.aliases);
            END;
            CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
                INSERT INTO entities_fts(entities_fts, rowid, slug, name, summary, aliases)
                VALUES ('delete', old.id, old.slug, old.name, old.summary, old.aliases);
            END;
            CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
                INSERT INTO entities_fts(entities_fts, rowid, slug, name, summary, aliases)
                VALUES ('delete', old.id, old.slug, old.name, old.summary, old.aliases);
                INSERT INTO entities_fts(rowid, slug, name, summary, aliases)
                VALUES (new.id, new.slug, new.name, new.summary, new.aliases);
            END;
        ''')
        self._conn.commit()

    def upsert(self, slug: str, type: str, name: str,
               wing: str | None = None, summary: str | None = None,
               attrs: dict | None = None, aliases: list | None = None) -> dict:
        """Insert or update an entity by slug. Returns the stored row as a dict."""
        now = _now()
        attrs_json = json.dumps(attrs or {}, sort_keys=True)
        aliases_json = json.dumps(aliases or [])
        cur = self._conn.cursor()
        existing = cur.execute('SELECT id, created FROM entities WHERE slug = ?', (slug,)).fetchone()
        if existing:
            cur.execute(
                '''UPDATE entities SET type=?, name=?, wing=?, summary=?, attrs=?, aliases=?, updated=?
                   WHERE slug=?''',
                (type, name, wing, summary, attrs_json, aliases_json, now, slug),
            )
        else:
            cur.execute(
                '''INSERT INTO entities (slug, type, name, wing, summary, attrs, aliases, created, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (slug, type, name, wing, summary, attrs_json, aliases_json, now, now),
            )
        self._conn.commit()
        return self.get(slug)

    def get(self, slug: str) -> dict | None:
        """Fetch a single entity by slug or alias."""
        row = self._conn.execute('SELECT * FROM entities WHERE slug = ?', (slug,)).fetchone()
        if row:
            return self._row_to_dict(row)
        # Fallback: alias lookup (LIKE over JSON array — small table, fine)
        rows = self._conn.execute(
            'SELECT * FROM entities WHERE aliases LIKE ?',
            (f'%"{slug}"%',),
        ).fetchall()
        if rows:
            return self._row_to_dict(rows[0])
        return None

    def list(self, type: str | None = None, wing: str | None = None) -> list[dict]:
        """List entities, optionally filtered by type and/or wing."""
        where = []
        params: list = []
        if type:
            where.append('type = ?')
            params.append(type)
        if wing:
            where.append('wing = ?')
            params.append(wing)
        sql = 'SELECT * FROM entities'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY type, slug'
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search(self, query: str, k: int = 10) -> list[dict]:
        """Fuzzy search over slug/name/summary/aliases via FTS5."""
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        rows = self._conn.execute(
            '''SELECT e.* FROM entities_fts f
               JOIN entities e ON e.id = f.rowid
               WHERE entities_fts MATCH ?
               ORDER BY f.rank
               LIMIT ?''',
            (fts_query, k),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, slug: str) -> bool:
        cur = self._conn.cursor()
        cur.execute('DELETE FROM entities WHERE slug = ?', (slug,))
        self._conn.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        return self._conn.execute('SELECT COUNT(*) FROM entities').fetchone()[0]

    def close(self):
        self._conn.close()

    @staticmethod
    def _fts_query(query: str) -> str:
        import re
        tokens = re.findall(r'[\w.-]+', query)
        return ' '.join(f'"{t}"' for t in tokens) if tokens else ''

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d['attrs'] = json.loads(d.get('attrs') or '{}')
        d['aliases'] = json.loads(d.get('aliases') or '[]')
        return d


if __name__ == '__main__':
    # Self-test against an in-memory DB.
    store = EntityStore(':memory:')
    store.upsert(
        slug='ct-111', type='container', name='CT 111 — notes-rag',
        wing='agents', summary='Hybrid FTS5+sqlite-vec RAG serving the workspace.',
        attrs={'ip': '192.168.88.71', 'port': 8080, 'service': 'rag.service'},
        aliases=['notes-rag', 'rag', '192.168.88.71'],
    )
    store.upsert(
        slug='ct-122', type='container', name='CT 122 — kanban + todos-indexer',
        wing='apps', summary='Kanban board frontend and todos-indexer API.',
        attrs={'ip': '192.168.88.78', 'ports': [3000, 8081]},
        aliases=['kanban', 'todos-indexer'],
    )
    store.upsert(
        slug='openclaw', type='service', name='OpenClaw Discord agent',
        wing='agents', summary='Discord bot running on CT 104, shares /mnt/Claude workspace.',
        attrs={'runs_on': 'ct-104', 'discord': True},
        aliases=['claw', 'openclaw-agent'],
    )
    print(f'count: {store.count()}')
    print()
    print('get(ct-111):', store.get('ct-111')['summary'])
    print('get(notes-rag) via alias:', store.get('notes-rag')['slug'])
    print()
    print('list(type=container):')
    for e in store.list(type='container'):
        print(f"  {e['slug']:10} {e['name']}")
    print()
    print('search("rag"):')
    for e in store.search('rag'):
        print(f"  {e['slug']:10} {e['name']}")
    print()
    print('search("discord bot"):')
    for e in store.search('discord bot'):
        print(f"  {e['slug']:10} {e['name']}")
