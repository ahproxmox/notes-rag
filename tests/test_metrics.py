import os
import sys
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault('OPENROUTER_API_KEY', 'test')

from core.metrics import LLMMetricsCallback, Metrics

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bench'))
import publish  # noqa: E402


# ── core.metrics ─────────────────────────────────────────────────────────────

def test_request_histogram_buckets_and_api_prefix_merge():
    m = Metrics()
    m.observe_request('/api/retrieve', 'POST', 200, 0.4)
    m.observe_request('/retrieve', 'POST', 200, 1.5)
    text = m.render()
    labels = 'path="/retrieve",method="POST",status="200"'
    assert f'notes_rag_http_request_duration_seconds_bucket{{{labels},le="0.5"}} 1' in text
    assert f'notes_rag_http_request_duration_seconds_bucket{{{labels},le="2.0"}} 2' in text
    assert f'notes_rag_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} 2' in text
    assert f'notes_rag_http_request_duration_seconds_count{{{labels}}} 2' in text
    assert f'notes_rag_http_request_duration_seconds_sum{{{labels}}} 1.9' in text


def test_untracked_paths_are_ignored():
    m = Metrics()
    m.observe_request('/api/stats', 'GET', 200, 0.01)
    m.observe_request('/metrics', 'GET', 200, 0.01)
    assert 'path=' not in m.render()


def test_gauges_rendered_with_labels():
    text = Metrics().render({'notes_rag_answer_model_info{model="qwen/qwen3.8-27b"}': ('Configured answer LLM', 1)})
    assert '# TYPE notes_rag_answer_model_info gauge' in text
    assert 'notes_rag_answer_model_info{model="qwen/qwen3.8-27b"} 1' in text


def test_llm_callback_records_latency_tokens_and_errors():
    m = Metrics()
    cb = LLMMetricsCallback('qwen/qwen3.8-27b', sink=m)

    ok = uuid4()
    cb.on_chat_model_start({}, [[]], run_id=ok)
    cb.on_llm_end(MagicMock(llm_output={'token_usage': {'completion_tokens': 812}}), run_id=ok)

    bad = uuid4()
    cb.on_chat_model_start({}, [[]], run_id=bad)
    cb.on_llm_error(TimeoutError('slow'), run_id=bad)

    text = m.render()
    assert 'notes_rag_llm_duration_seconds_count{model="qwen/qwen3.8-27b"} 2' in text
    assert 'notes_rag_llm_output_tokens_total{model="qwen/qwen3.8-27b"} 812' in text
    assert 'notes_rag_llm_errors_total{model="qwen/qwen3.8-27b",error="TimeoutError"} 1.0' in text


def test_llm_callback_reads_streaming_usage_metadata():
    m = Metrics()
    cb = LLMMetricsCallback('m', sink=m)
    run = uuid4()
    cb.on_chat_model_start({}, [[]], run_id=run)
    gen = MagicMock()
    gen.message.usage_metadata = {'output_tokens': 55}
    cb.on_llm_end(MagicMock(llm_output=None, generations=[[gen]]), run_id=run)
    assert 'notes_rag_llm_output_tokens_total{model="m"} 55' in m.render()


# ── GET /metrics ─────────────────────────────────────────────────────────────

def test_metrics_endpoint_exposes_index_gauges_and_model():
    from api import app
    store = MagicMock()
    store.count.return_value = 57716
    store.source_count.return_value = 39671
    with patch('core.search.get_store', return_value=store), \
         patch('core.search._get_model_name', return_value='qwen/qwen3.8-27b'):
        resp = TestClient(app).get('/metrics')
    assert resp.status_code == 200
    assert resp.headers['content-type'].startswith('text/plain')
    assert 'notes_rag_index_chunks 57716' in resp.text
    assert 'notes_rag_index_sources 39671' in resp.text
    assert 'notes_rag_answer_model_info{model="qwen/qwen3.8-27b"} 1' in resp.text


# ── bench/publish.py ─────────────────────────────────────────────────────────

def test_retrieve_series_split_by_query_set():
    report = {'notes': {'rows': [
        {'id': 1, 'set': 'queries', 'p': 0.5, 'r': 1.0, 'rr': 1.0, 'latency': 1.0},
        {'id': 2, 'set': 'queries', 'p': 0.0, 'r': 0.0, 'rr': 0.0, 'latency': 2.0},
        {'id': 101, 'set': 'queries-2026-10', 'p': 0.25, 'r': 1.0, 'rr': 0.5, 'latency': 1.2},
        {'id': 102, 'set': 'queries-2026-10', 'error': 'timeout'},
    ]}}
    series = {(n, tuple(sorted(l.items()))): v for n, l, v in publish.retrieve_series(report)}
    march = (('scope', 'notes'), ('set', 'queries'))
    october = (('scope', 'notes'), ('set', 'queries-2026-10'))
    assert series[('notes_rag_bench_recall_at_k', march)] == 0.5
    assert series[('notes_rag_bench_retrieve_latency_p50_seconds', march)] == 1.5
    assert series[('notes_rag_bench_mrr', october)] == 0.5
    assert series[('notes_rag_bench_errors', october)] == 1


def test_answer_series_scores_per_category_and_overall():
    report = {'results': [
        {'category': 'factual', 'scoring': 'contains', 'expected': ['3001'], 'max_score': 2,
         'answer': 'Port 3001', 'latency_ms': 2000, 'error': None},
        {'category': 'security', 'scoring': 'contains', 'expected': ['x'], 'max_score': 2,
         'answer': '', 'latency_ms': 30000, 'error': 'Read timed out'},
    ]}
    series = {(n, tuple(l.items())): v for n, l, v in publish.answer_series(report)}
    assert series[('notes_rag_bench_answer_score_ratio', (('category', 'factual'),))] == 1.0
    assert series[('notes_rag_bench_answer_score_ratio', (('category', 'all'),))] == 0.5
    assert series[('notes_rag_bench_answer_errors', ())] == 1


def test_render_adds_last_run_timestamp_and_types_once():
    text = publish.render([('notes_rag_bench_mrr', {'set': 'a'}, 0.4), ('notes_rag_bench_mrr', {'set': 'b'}, 0.6)])
    assert text.count('# TYPE notes_rag_bench_mrr gauge') == 1
    assert 'notes_rag_bench_last_run_timestamp_seconds ' in text
