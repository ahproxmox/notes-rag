"""Request and LLM metrics, scraped by Prometheus (CT 118) from GET /metrics.

Hand-written exposition text, same as core/health.py: no prometheus_client
dependency for a handful of histograms and counters.

- notes_rag_http_request_duration_seconds{path,method,status}: search-type
  endpoints only (TRACKED_PATHS). For /search/stream this is time to the first
  byte, not to the end of the stream; the LLM histogram covers the rest.
- notes_rag_llm_duration_seconds{model}, notes_rag_llm_errors_total{model,error},
  notes_rag_llm_output_tokens_total{model}: every call made through
  core.search._get_llm (answers, recent-activity summaries, streaming).
"""

import threading
import time
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 20.0, 30.0, 60.0)

# Paths are recorded without the /api prefix so /api/retrieve and the /retrieve alias share a series.
TRACKED_PATHS = {'/retrieve', '/search', '/search/stream', '/notes/search', '/similar'}


def _escape(value: str) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = '') -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values)]
    if extra:
        parts.append(extra)
    return '{' + ','.join(parts) + '}' if parts else ''


class _Histogram:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...]):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._series: dict[tuple[str, ...], list] = {}  # labels -> [bucket counts, sum, count]

    def observe(self, labels: tuple[str, ...], value: float):
        s = self._series.setdefault(labels, [[0] * len(LATENCY_BUCKETS), 0.0, 0])
        for i, bound in enumerate(LATENCY_BUCKETS):
            if value <= bound:
                s[0][i] += 1
        s[1] += value
        s[2] += 1

    def render(self) -> list[str]:
        lines = [f'# HELP {self.name} {self.help}', f'# TYPE {self.name} histogram']
        for labels, (buckets, total, count) in sorted(self._series.items()):
            for bound, n in zip(LATENCY_BUCKETS, buckets):
                le = 'le="%s"' % bound
                lines.append(f'{self.name}_bucket{_labels(self.label_names, labels, le)} {n}')
            le = 'le="+Inf"'
            lines.append(f'{self.name}_bucket{_labels(self.label_names, labels, le)} {count}')
            lines.append(f'{self.name}_sum{_labels(self.label_names, labels)} {total}')
            lines.append(f'{self.name}_count{_labels(self.label_names, labels)} {count}')
        return lines


class _Counter:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...]):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._series: dict[tuple[str, ...], float] = {}

    def inc(self, labels: tuple[str, ...], amount: float = 1.0):
        self._series[labels] = self._series.get(labels, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f'# HELP {self.name} {self.help}', f'# TYPE {self.name} counter']
        for labels, value in sorted(self._series.items()):
            lines.append(f'{self.name}{_labels(self.label_names, labels)} {value}')
        return lines


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.requests = _Histogram('notes_rag_http_request_duration_seconds',
                                   'Search endpoint latency', ('path', 'method', 'status'))
        self.llm = _Histogram('notes_rag_llm_duration_seconds',
                              'Answer LLM call latency', ('model',))
        self.llm_errors = _Counter('notes_rag_llm_errors_total',
                                   'Answer LLM calls that raised', ('model', 'error'))
        self.llm_tokens = _Counter('notes_rag_llm_output_tokens_total',
                                   'Answer LLM output tokens, reasoning included', ('model',))

    def observe_request(self, path: str, method: str, status: int, seconds: float):
        if path.startswith('/api/'):
            path = path[len('/api'):]
        if path not in TRACKED_PATHS:
            return
        with self._lock:
            self.requests.observe((path, method, str(status)), seconds)

    def observe_llm(self, model: str, seconds: float, output_tokens: int | None = None,
                    error: str | None = None):
        with self._lock:
            self.llm.observe((model,), seconds)
            if output_tokens:
                self.llm_tokens.inc((model,), output_tokens)
            if error:
                self.llm_errors.inc((model, error))

    def render(self, gauges: dict[str, tuple[str, float]] | None = None) -> str:
        """Exposition text. gauges maps a full series name (labels included) to (help, value)."""
        with self._lock:
            lines = (self.requests.render() + self.llm.render()
                     + self.llm_errors.render() + self.llm_tokens.render())
        for series, (help_text, value) in (gauges or {}).items():
            name = series.split('{', 1)[0]
            lines += [f'# HELP {name} {help_text}', f'# TYPE {name} gauge', f'{series} {value}']
        return '\n'.join(lines) + '\n'


metrics = Metrics()


class LLMMetricsCallback(BaseCallbackHandler):
    """LangChain callback that times each chat-model call and records it in `metrics`."""

    def __init__(self, model: str, sink: Metrics = metrics):
        self.model = model
        self.sink = sink
        self._started: dict[UUID, float] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs):
        self._started[run_id] = time.monotonic()

    def on_llm_end(self, response, *, run_id: UUID, **kwargs):
        start = self._started.pop(run_id, None)
        if start is None:
            return
        self.sink.observe_llm(self.model, time.monotonic() - start, _output_tokens(response))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs):
        start = self._started.pop(run_id, None)
        if start is None:
            return
        self.sink.observe_llm(self.model, time.monotonic() - start, error=type(error).__name__)


def _output_tokens(response) -> int | None:
    usage = (response.llm_output or {}).get('token_usage') or {}
    if usage.get('completion_tokens'):
        return usage['completion_tokens']
    # Streaming responses carry usage on the message instead of llm_output.
    try:
        meta = response.generations[0][0].message.usage_metadata or {}
        return meta.get('output_tokens')
    except (IndexError, AttributeError):
        return None
