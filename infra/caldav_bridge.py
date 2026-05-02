"""
Reminders queue — buffers create-reminder requests for the Mac bridge to pick up.
CalDAV removed; Mac osascript bridge handles native Reminders creation.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(os.path.dirname(os.path.dirname(__file__))) / 'reminders.db'


def _init_db():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id    TEXT NOT NULL,
            title      TEXT NOT NULL,
            due_iso    TEXT,
            notes      TEXT DEFAULT \'\',
            created_at TEXT NOT NULL,
            status     TEXT DEFAULT \'pending\'
        )
    ''')
    conn.commit()
    conn.close()


def create_reminder(title: str, todo_id, due_dt=None, notes: str = '') -> bool:
    due_iso = due_dt.isoformat() if due_dt else None
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        'INSERT INTO queue (todo_id, title, due_iso, notes, created_at) VALUES (?,?,?,?,?)',
        (str(todo_id), title, due_iso, notes, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return True


def start_poller():
    _init_db()
