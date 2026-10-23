#!/usr/bin/env python3
"""
Push a bench result file to the Pushgateway (CT 118) for the Grafana RAG dashboard.

Usage:
    python bench/publish.py retrieve bench/results/scheduled-retrieve.json   # from retrieve_bench.py --output
    python bench/publish.py answer   bench/results/scheduled-answer.json     # from run_bench.py --output

Each kind is its own Pushgateway group (job=notes_rag_bench, kind=...), so a
nightly retrieve push never wipes the weekly answer series. PUSHGATEWAY_URL
overrides the default. Exits non-zero if the push fails.
"""

import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from score import score_result

DEFAULT_PUSHGATEWAY_URL = 'http://192.168.88.73:9091'
JOB = 'notes_rag_bench'


def retrieve_series(report: dict) -> list[tuple[str, dict, float]]:
    """(metric, labels, value) per query set and scope from a retrieve_bench.py report."""
    series = []
    for scope, data in report.items():
        by_set = defaultdict(list)
        for row in data['rows']:
            by_set[row['set']].append(row)
        for qset, rows in by_set.items():
            ok = [r for r in rows if 'error' not in r]
            labels = {'set': qset, 'scope': scope}
            series.append(('notes_rag_bench_queries', labels, len(ok)))
            series.append(('notes_rag_bench_errors', labels, len(rows) - len(ok)))
            if not ok:
                continue
            series += [
                ('notes_rag_bench_recall_at_k', labels, statistics.mean(r['r'] for r in ok)),
                ('notes_rag_bench_mrr', labels, statistics.mean(r['rr'] for r in ok)),
                ('notes_rag_bench_precision_at_k', labels, statistics.mean(r['p'] for r in ok)),
                ('notes_rag_bench_retrieve_latency_p50_seconds', labels,
                 statistics.median(r['latency'] for r in ok)),
            ]
    return series


def answer_series(report: dict) -> list[tuple[str, dict, float]]:
    """Answer score per category (plus category="all") and latency from a run_bench.py report."""
    got, possible = defaultdict(int), defaultdict(int)
    for r in report['results']:
        score, _ = score_result(r)
        for cat in (r.get('category') or 'uncategorised', 'all'):
            got[cat] += score
            possible[cat] += r['max_score']
    series = [('notes_rag_bench_answer_score_ratio', {'category': c}, got[c] / possible[c])
              for c in possible if possible[c]]
    lat = sorted(r['latency_ms'] / 1000 for r in report['results'])
    series += [
        ('notes_rag_bench_answer_latency_p50_seconds', {}, statistics.median(lat)),
        ('notes_rag_bench_answer_latency_p90_seconds', {}, lat[int(len(lat) * 0.9)]),
        ('notes_rag_bench_answer_errors', {}, sum(1 for r in report['results'] if r.get('error'))),
    ]
    return series


def render(series: list[tuple[str, dict, float]]) -> str:
    series = series + [('notes_rag_bench_last_run_timestamp_seconds', {}, time.time())]
    lines, typed = [], set()
    for name, labels, value in series:
        if name not in typed:
            lines.append(f'# TYPE {name} gauge')
            typed.add(name)
        label_text = ','.join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f'{name}{{{label_text}}} {value}' if label_text else f'{name} {value}')
    return '\n'.join(lines) + '\n'


def push(text: str, kind: str):
    url = os.environ.get('PUSHGATEWAY_URL', DEFAULT_PUSHGATEWAY_URL)
    req = urllib.request.Request(
        f'{url.rstrip("/")}/metrics/job/{JOB}/kind/{kind}',
        data=text.encode(), method='PUT',
        headers={'Content-Type': 'text/plain; version=0.0.4'},
    )
    urllib.request.urlopen(req, timeout=10).close()


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('retrieve', 'answer'):
        sys.exit(__doc__)
    kind, path = sys.argv[1], sys.argv[2]
    report = json.load(open(path))
    series = retrieve_series(report) if kind == 'retrieve' else answer_series(report)
    text = render(series)
    push(text, kind)
    print(f'pushed {len(series)} series (job={JOB}, kind={kind})')


if __name__ == '__main__':
    main()
