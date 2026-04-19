"""
CalDAV bridge — Apple Reminders integration via iCloud CalDAV.
Creates VTODO items on iCloud and polls every 5 min for completions.
"""
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar, Todo

log = logging.getLogger(__name__)

_MELB = ZoneInfo("Australia/Melbourne")
_TODOS_DIR = Path("/mnt/Claude/todos")
_DB_PATH = Path(os.path.dirname(__file__)) / "reminders.db"
_POLL_INTERVAL = 300
_ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"

_calendar = None
_calendar_lock = threading.Lock()


def _init_db():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            uid        TEXT PRIMARY KEY,
            todo_id    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _db_insert(uid, todo_id):
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO reminders (uid, todo_id, created_at) VALUES (?, ?, ?)",
        (uid, str(todo_id), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _db_all():
    conn = sqlite3.connect(_DB_PATH)
    rows = {r[0]: r[1] for r in conn.execute("SELECT uid, todo_id FROM reminders")}
    conn.close()
    return rows


def _db_delete(uid):
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("DELETE FROM reminders WHERE uid = ?", (uid,))
    conn.commit()
    conn.close()


def _get_calendar():
    global _calendar
    with _calendar_lock:
        if _calendar is not None:
            return _calendar
        apple_id = os.environ.get("CALDAV_APPLE_ID", "")
        app_password = os.environ.get("CALDAV_APP_PASSWORD", "")
        if not apple_id or not app_password:
            log.warning("[caldav] CALDAV_APPLE_ID / CALDAV_APP_PASSWORD not set")
            return None
        try:
            client = caldav.DAVClient(
                url=_ICLOUD_CALDAV_URL,
                username=apple_id,
                password=app_password,
            )
            principal = client.principal()
            for cal in principal.calendars():
                try:
                    name = cal.name or ""
                    if "Reminder" in name:
                        _calendar = cal
                        log.info(f"[caldav] connected: {name}")
                        return _calendar
                except Exception:
                    continue
            for cal in principal.calendars():
                try:
                    cal.todos()
                    _calendar = cal
                    log.info("[caldav] using fallback calendar")
                    return _calendar
                except Exception:
                    continue
            log.error("[caldav] no suitable calendar found")
        except Exception as e:
            log.error(f"[caldav] connection failed: {e}")
        return None


def create_reminder(title, todo_id, due_dt=None, notes=""):
    cal = _get_calendar()
    if cal is None:
        return False

    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    vcal = Calendar()
    vcal.add("prodid", "-//notes-rag//caldav-bridge//EN")
    vcal.add("version", "2.0")

    vtodo = Todo()
    vtodo.add("uid", uid)
    vtodo.add("summary", title)
    vtodo.add("dtstamp", now)
    vtodo.add("created", now)
    desc = f"[todo-{todo_id}]"
    if notes:
        desc += f"\n{notes}"
    vtodo.add("description", desc)
    if due_dt:
        vtodo.add("due", due_dt)

    vcal.add_component(vtodo)

    try:
        cal.save_todo(ical=vcal.to_ical().decode())
        _db_insert(uid, str(todo_id))
        log.info(f"[caldav] reminder created uid={uid} todo={todo_id}")
        return True
    except Exception as e:
        log.error(f"[caldav] create_reminder failed: {e}")
        return False


def _mark_todo_complete(todo_id):
    padded = str(todo_id).zfill(3)
    for pattern in (f"{padded}-*.md", f"{todo_id}-*.md"):
        for path in _TODOS_DIR.glob(pattern):
            text = path.read_text(encoding="utf-8")
            if "status: pending" in text:
                path.write_text(text.replace("status: pending", "status: completed", 1), encoding="utf-8")
                log.info(f"[caldav] todo {todo_id} marked completed")
            return


def _poll():
    global _calendar
    try:
        cal = _get_calendar()
        if cal is None:
            return
        tracked = _db_all()
        if not tracked:
            return
        for item in cal.todos(include_completed=True):
            try:
                comp = item.icalendar_component
                uid = str(comp.get("uid", ""))
                status = str(comp.get("status", "")).upper()
                if uid in tracked and status == "COMPLETED":
                    _mark_todo_complete(tracked[uid])
                    _db_delete(uid)
            except Exception as e:
                log.debug(f"[caldav] poll item error: {e}")
    except Exception as e:
        log.error(f"[caldav] poll error: {e}")
        with _calendar_lock:
            _calendar = None


def _poll_loop():
    while True:
        _poll()
        threading.Event().wait(_POLL_INTERVAL)


def start_poller():
    _init_db()
    t = threading.Thread(target=_poll_loop, daemon=True, name="caldav-poller")
    t.start()
    log.info("[caldav] poller started (interval=5min)")
