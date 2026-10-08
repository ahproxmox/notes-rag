"""Indexing health: failure/success counters, queue depth, recent failures.

Surfaced on /health and /stats, and pushed to the Pushgateway (CT 118) so
indexing errors are visible instead of only landing in the journal.

PUSHGATEWAY_URL overrides the default; set it to an empty string to disable.
"""

import os
import threading
import time
import urllib.request
from collections import deque

DEFAULT_PUSHGATEWAY_URL = 'http://192.168.88.73:9091'
JOB = 'notes_rag'
MAX_RECENT_FAILURES = 20
SUCCESS_PUSH_INTERVAL = 60.0   # seconds; failures always push immediately
DEGRADED_WINDOW = 900.0        # a failure within the last 15 min => degraded


class IndexHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self._successes = 0
        self._failures = 0
        self._push_failures = 0
        self._last_failure_ts = None
        self._recent = deque(maxlen=MAX_RECENT_FAILURES)
        self._queue_depth_fn = None
        self._last_push = 0.0

    def set_queue_depth_fn(self, fn):
        self._queue_depth_fn = fn

    def record_success(self, path=None):
        with self._lock:
            self._successes += 1
        self._maybe_push(force=False)

    def record_failure(self, path, error):
        now = time.time()
        with self._lock:
            self._failures += 1
            self._last_failure_ts = now
            self._recent.append({'path': str(path), 'error': str(error), 'ts': now})
        self._maybe_push(force=True)

    def snapshot(self) -> dict:
        depth = self._queue_depth_fn() if self._queue_depth_fn else 0
        with self._lock:
            last = self._last_failure_ts
            return {
                'successes': self._successes,
                'failures': self._failures,
                'push_failures': self._push_failures,
                'queue_depth': depth,
                'last_failure_ts': last,
                'degraded': bool(last and time.time() - last < DEGRADED_WINDOW),
                'recent_failures': list(self._recent),
            }

    def _metrics_text(self) -> str:
        s = self.snapshot()
        lines = [
            '# TYPE notes_rag_index_success_total counter',
            f'notes_rag_index_success_total {s["successes"]}',
            '# TYPE notes_rag_index_failures_total counter',
            f'notes_rag_index_failures_total {s["failures"]}',
            '# TYPE notes_rag_index_queue_depth gauge',
            f'notes_rag_index_queue_depth {s["queue_depth"]}',
            '# TYPE notes_rag_index_last_failure_timestamp_seconds gauge',
            f'notes_rag_index_last_failure_timestamp_seconds {s["last_failure_ts"] or 0}',
        ]
        return '\n'.join(lines) + '\n'

    def _maybe_push(self, force: bool):
        url = os.environ.get('PUSHGATEWAY_URL', DEFAULT_PUSHGATEWAY_URL)
        if not url:
            return
        now = time.time()
        with self._lock:
            if not force and now - self._last_push < SUCCESS_PUSH_INTERVAL:
                return
            self._last_push = now
        # Off-thread so a slow/unreachable gateway never stalls the index worker.
        threading.Thread(target=self._push, args=(url,), daemon=True).start()

    def _push(self, url: str):
        req = urllib.request.Request(
            f'{url.rstrip("/")}/metrics/job/{JOB}',
            data=self._metrics_text().encode(), method='POST',
            headers={'Content-Type': 'text/plain; version=0.0.4'},
        )
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            with self._lock:
                self._push_failures += 1
            print(f'[health] pushgateway push failed: {e}', flush=True)


health = IndexHealth()
