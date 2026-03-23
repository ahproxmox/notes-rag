#!/usr/bin/env python3
"""
Weight sweep — test multiple BM25/vector weight combinations.

Usage:
    python bench/sweep.py --endpoint http://192.168.88.71:8080
    python bench/sweep.py --endpoint http://192.168.88.71:8080 --output bench/sweep-results/2026-03-23.json
"""

import argparse
import json
import os
import time

from run_bench import load_queries, run_query
from score import score_result, compute_ir_metrics

WEIGHT_GRID = [
    (0.0, 1.0),   # vector only
    (0.2, 0.8),
    (0.4, 0.6),   # current default
    (0.5, 0.5),
    (0.6, 0.4),
    (0.8, 0.2),
    (1.0, 0.0),   # BM25 only
]


def run_sweep(endpoint, queries, weight_grid):
    """Run all queries for each weight combination."""
    runs = []

    for bm25_w, vec_w in weight_grid:
        print(f'\n── BM25={bm25_w:.1f} / Vector={vec_w:.1f} ──')
        results = []
        total_score = 0
        total_max = 0

        for q in queries:
            qid = q['id']
            print(f'  [{qid:2d}] {q["query"][:50]}...', end=' ', flush=True)

            result = run_query(endpoint, q['query'],
                               bm25_weight=bm25_w, vector_weight=vec_w)
            result['id'] = qid
            result['query'] = q['query']
            result['category'] = q.get('category', '')
            result['expected'] = q['expected']
            result['expected_sources'] = q.get('expected_sources', [])
            result['max_score'] = q['max_score']
            result['scoring'] = q['scoring']
            results.append(result)

            score, _ = score_result(result)
            total_score += score
            total_max += q['max_score']

            status = 'ERROR' if result['error'] else 'OK'
            print(f'{status} ({result["latency_ms"]:.0f}ms)')

        ir = compute_ir_metrics(results)
        answer_pct = (total_score / total_max * 100) if total_max else 0
        avg_latency = sum(r['latency_ms'] for r in results) / len(results)

        run = {
            'bm25_weight': bm25_w,
            'vector_weight': vec_w,
            'answer_score': f'{total_score}/{total_max}',
            'answer_pct': round(answer_pct, 1),
            'avg_latency_ms': round(avg_latency, 1),
            'results': results,
        }
        if ir:
            run['precision_at_k'] = round(ir['precision_at_k'], 3)
            run['recall_at_k'] = round(ir['recall_at_k'], 3)
            run['mrr'] = round(ir['mrr'], 3)

        runs.append(run)

    return runs


def print_summary(runs):
    """Print a ranked summary table."""
    print('\n' + '=' * 90)
    print('SWEEP SUMMARY')
    print(f'{"BM25":>6}  {"Vector":>6}  {"Answer":>10}  {"P@k":>6}  {"R@k":>6}  {"MRR":>6}  {"Latency":>8}')
    print('-' * 90)

    # Sort by composite score: 0.5*answer + 0.3*recall + 0.2*mrr
    def composite(r):
        return (0.5 * r['answer_pct'] / 100
                + 0.3 * r.get('recall_at_k', 0)
                + 0.2 * r.get('mrr', 0))

    for r in sorted(runs, key=composite, reverse=True):
        marker = ' ◀ current' if r['bm25_weight'] == 0.4 and r['vector_weight'] == 0.6 else ''
        print(f'{r["bm25_weight"]:>6.1f}  {r["vector_weight"]:>6.1f}  '
              f'{r["answer_score"]:>10}  '
              f'{r.get("precision_at_k", 0):>6.3f}  '
              f'{r.get("recall_at_k", 0):>6.3f}  '
              f'{r.get("mrr", 0):>6.3f}  '
              f'{r["avg_latency_ms"]:>7.0f}ms{marker}')

    print('-' * 90)
    best = max(runs, key=composite)
    print(f'Best: BM25={best["bm25_weight"]:.1f} / Vector={best["vector_weight"]:.1f} '
          f'(Answer={best["answer_pct"]:.0f}%, R@k={best.get("recall_at_k", 0):.3f}, '
          f'MRR={best.get("mrr", 0):.3f})')


def main():
    parser = argparse.ArgumentParser(description='RAG weight sweep')
    parser.add_argument('--endpoint', default='http://localhost:8080',
                        help='RAG API base URL')
    parser.add_argument('--output', default=None,
                        help='Path to save sweep results JSON')
    args = parser.parse_args()

    queries = load_queries()
    print(f'Weight sweep: {len(WEIGHT_GRID)} combinations × {len(queries)} queries '
          f'= {len(WEIGHT_GRID) * len(queries)} total requests\n')

    runs = run_sweep(args.endpoint, queries, WEIGHT_GRID)
    print_summary(runs)

    output = {
        'endpoint': args.endpoint,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'query_count': len(queries),
        'weight_combos': len(WEIGHT_GRID),
        'runs': runs,
    }

    output_path = args.output or os.path.join(
        os.path.dirname(__file__), 'sweep-results',
        time.strftime('%Y-%m-%d.json'))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f'\nResults saved to {output_path}')


if __name__ == '__main__':
    main()
