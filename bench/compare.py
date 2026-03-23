#!/usr/bin/env python3
"""
Compare two RAG evaluation result files side-by-side.

Usage:
    python bench/compare.py bench/results/baseline.json bench/results/after-change.json
"""

import json
import sys

from score import score_result, compute_ir_metrics


def load_results(path):
    with open(path) as f:
        return json.load(f)


def compare(a, b):
    """Compare two result sets and print differences."""
    print(f'Comparing:')
    print(f'  A: {a.get("endpoint", "?")} @ {a.get("timestamp", "?")}')
    if 'bm25_weight' in a:
        print(f'     Weights: BM25={a["bm25_weight"]}, Vector={a["vector_weight"]}')
    print(f'  B: {b.get("endpoint", "?")} @ {b.get("timestamp", "?")}')
    if 'bm25_weight' in b:
        print(f'     Weights: BM25={b["bm25_weight"]}, Vector={b["vector_weight"]}')
    print()

    a_results = {r['id']: r for r in a['results']}
    b_results = {r['id']: r for r in b['results']}
    all_ids = sorted(set(a_results.keys()) | set(b_results.keys()))

    # ── Aggregate metrics ───────────────────────────────────────────────
    a_total = sum(score_result(a_results[i])[0] for i in all_ids if i in a_results)
    b_total = sum(score_result(b_results[i])[0] for i in all_ids if i in b_results)
    a_max = sum(a_results[i]['max_score'] for i in all_ids if i in a_results)
    b_max = sum(b_results[i]['max_score'] for i in all_ids if i in b_results)

    a_pct = (a_total / a_max * 100) if a_max else 0
    b_pct = (b_total / b_max * 100) if b_max else 0

    a_ir = compute_ir_metrics(a.get('results', []))
    b_ir = compute_ir_metrics(b.get('results', []))

    print('AGGREGATE')
    print(f'  {"Metric":<15}  {"A":>10}  {"B":>10}  {"Delta":>10}')
    print(f'  {"-"*50}')
    print(f'  {"Answer Score":<15}  {a_total}/{a_max:>5}  {b_total}/{b_max:>5}  {b_total-a_total:>+10d}')
    print(f'  {"Answer %":<15}  {a_pct:>10.1f}  {b_pct:>10.1f}  {b_pct-a_pct:>+10.1f}')

    if a_ir and b_ir:
        for metric in ['precision_at_k', 'recall_at_k', 'mrr']:
            label = metric.upper().replace('_AT_K', '@k')
            av = a_ir[metric]
            bv = b_ir[metric]
            print(f'  {label:<15}  {av:>10.3f}  {bv:>10.3f}  {bv-av:>+10.3f}')

    a_lat = sum(r['latency_ms'] for r in a['results']) / len(a['results']) if a['results'] else 0
    b_lat = sum(r['latency_ms'] for r in b['results']) / len(b['results']) if b['results'] else 0
    print(f'  {"Avg Latency":<15}  {a_lat:>9.0f}ms {b_lat:>9.0f}ms {b_lat-a_lat:>+9.0f}ms')

    # ── Per-query deltas ────────────────────────────────────────────────
    improved = []
    regressed = []
    changed_sources = []

    for qid in all_ids:
        if qid not in a_results or qid not in b_results:
            continue
        ar = a_results[qid]
        br = b_results[qid]
        a_score, _ = score_result(ar)
        b_score, _ = score_result(br)

        if b_score > a_score:
            improved.append((qid, ar['query'], a_score, b_score))
        elif b_score < a_score:
            regressed.append((qid, ar['query'], a_score, b_score))

        if set(ar.get('sources', [])) != set(br.get('sources', [])):
            changed_sources.append(qid)

    if improved:
        print(f'\nIMPROVED ({len(improved)})')
        for qid, query, a_s, b_s in improved:
            print(f'  [{qid:2d}] {query[:50]} — {a_s} → {b_s}')

    if regressed:
        print(f'\nREGRESSED ({len(regressed)})')
        for qid, query, a_s, b_s in regressed:
            print(f'  [{qid:2d}] {query[:50]} — {a_s} → {b_s}')

    if not improved and not regressed:
        print(f'\nNo score changes between A and B.')

    if changed_sources:
        print(f'\nSOURCE CHANGES ({len(changed_sources)} queries): {changed_sources}')


def main():
    if len(sys.argv) < 3:
        print('Usage: python bench/compare.py <results-a.json> <results-b.json>')
        sys.exit(1)

    a = load_results(sys.argv[1])
    b = load_results(sys.argv[2])
    compare(a, b)


if __name__ == '__main__':
    main()
