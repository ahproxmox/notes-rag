#!/usr/bin/env python3
"""Retrieval-only eval against POST /retrieve: no LLM, so fast and deterministic.

Scores each query's `expected_sources` with the same IR metrics as score.py
(P@k, recall@k, MRR), once per --scope, and reports latency. Use it to compare
scopes (todo 532) or a model/prefix change against a baseline (todo 533).

    python bench/retrieve_bench.py --endpoint http://192.168.88.71:8080 --scope notes --scope all
    python bench/retrieve_bench.py --queries bench/queries.yaml --queries bench/queries-2026-10.yaml --output out.json
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import precision_at_k, recall_at_k, reciprocal_rank  # noqa: E402

DEFAULT_QUERIES = str(Path(__file__).resolve().parent / 'queries.yaml')


def load_queries(paths):
    out = []
    for p in paths:
        for q in yaml.safe_load(open(p))['queries']:
            if q.get('expected_sources'):
                out.append({**q, 'set': Path(p).stem})
    return out


def retrieve(endpoint, query, scope, k):
    t0 = time.perf_counter()
    resp = requests.post(f'{endpoint}/retrieve', json={'query': query, 'k': k, 'scope': scope}, timeout=30)
    resp.raise_for_status()
    elapsed = time.perf_counter() - t0
    # Unique filenames in rank order (several chunks can come from one file).
    sources = list(dict.fromkeys(c['source'] for c in resp.json()['chunks']))
    return sources, elapsed


def run(endpoint, queries, scope, k):
    rows = []
    for q in queries:
        try:
            sources, elapsed = retrieve(endpoint, q['query'], scope, k)
        except Exception as e:  # noqa: BLE001 - reported per query, counted below
            rows.append({'id': q['id'], 'set': q['set'], 'error': str(e)})
            continue
        exp = q['expected_sources']
        rows.append({
            'id': q['id'], 'set': q['set'], 'category': q.get('category'), 'query': q['query'],
            'expected': exp, 'sources': sources, 'latency': elapsed,
            'p': precision_at_k(sources, exp), 'r': recall_at_k(sources, exp), 'rr': reciprocal_rank(sources, exp),
        })
    return rows


def summarise(rows):
    ok = [r for r in rows if 'error' not in r]
    if not ok:
        return {'count': 0, 'errors': len(rows)}
    lat = sorted(r['latency'] for r in ok)
    return {
        'count': len(ok),
        'errors': len(rows) - len(ok),
        'precision_at_k': statistics.mean(r['p'] for r in ok),
        'recall_at_k': statistics.mean(r['r'] for r in ok),
        'mrr': statistics.mean(r['rr'] for r in ok),
        'latency_p50': statistics.median(lat),
        'latency_max': lat[-1],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--endpoint', default='http://localhost:8080')
    ap.add_argument('--queries', action='append', help=f'queries YAML (repeatable; default {DEFAULT_QUERIES})')
    ap.add_argument('--scope', action='append', choices=['notes', 'news', 'all'], help='repeatable; default notes')
    ap.add_argument('--k', type=int, default=8)
    ap.add_argument('--output', help='write per-query rows + summaries as JSON')
    args = ap.parse_args()

    queries = load_queries(args.queries or [DEFAULT_QUERIES])
    report = {}
    for scope in args.scope or ['notes']:
        rows = run(args.endpoint, queries, scope, args.k)
        report[scope] = {'summary': summarise(rows), 'rows': rows}
        s = report[scope]['summary']
        print(f"scope={scope:<6} n={s['count']} errors={s['errors']} "
              f"P@k={s.get('precision_at_k', 0):.3f} R@k={s.get('recall_at_k', 0):.3f} "
              f"MRR={s.get('mrr', 0):.3f} p50={s.get('latency_p50', 0):.2f}s max={s.get('latency_max', 0):.2f}s")
        for r in rows:
            if 'error' in r:
                print(f"  ERROR q{r['id']} ({r['set']}): {r['error']}")

    if len(report) > 1:
        print('\nPer-query recall differences:')
        scopes = list(report)
        by_id = {s: {(r['set'], r['id']): r for r in report[s]['rows'] if 'error' not in r} for s in scopes}
        for key in by_id[scopes[0]]:
            vals = [by_id[s].get(key, {}).get('r') for s in scopes]
            if len({v for v in vals if v is not None}) > 1:
                print(f"  {key[0]} q{key[1]}: " + '  '.join(f'{s}={v:.2f}' for s, v in zip(scopes, vals) if v is not None))

    if args.output:
        json.dump(report, open(args.output, 'w'), indent=2)
        print(f'\nwrote {args.output}')


if __name__ == '__main__':
    main()
