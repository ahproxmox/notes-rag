#!/usr/bin/env python3
"""
Chunk size sweep — compare evaluation results across different chunk sizes.

This script runs the evaluation side only. The index must be rebuilt externally
(via MCP or SSH) between runs. Use with the manual workflow:

    1. Update chunk_size in indexer.yaml on CT 111
    2. Rebuild index: python /opt/rag/indexer.py
    3. Restart service: systemctl restart rag.service
    4. Run: python bench/chunk_sweep.py --endpoint http://192.168.88.71:8080 --chunk-size 500

Results are saved to bench/sweep-results/chunk-{size}.json and can be compared
with bench/compare.py.

Full automated sweep (after each manual rebuild):
    python bench/chunk_sweep.py --endpoint http://192.168.88.71:8080 --chunk-size 300
    python bench/chunk_sweep.py --endpoint http://192.168.88.71:8080 --chunk-size 500
    python bench/chunk_sweep.py --endpoint http://192.168.88.71:8080 --chunk-size 800
    python bench/chunk_sweep.py --endpoint http://192.168.88.71:8080 --chunk-size 1200
    python bench/chunk_sweep.py --compare
"""

import argparse
import json
import os
import sys
import time

from run_bench import load_queries, run_query
from score import score_result, compute_ir_metrics


RESULTS_DIR = os.path.join(os.path.dirname(__file__), 'sweep-results')


def run_eval(endpoint, chunk_size, queries):
    """Run all queries and save results tagged with chunk_size."""
    results = []
    total_score = 0
    total_max = 0

    for q in queries:
        qid = q['id']
        print(f'  [{qid:2d}] {q["query"][:50]}...', end=' ', flush=True)

        result = run_query(endpoint, q['query'])
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

    output = {
        'endpoint': endpoint,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'chunk_size': chunk_size,
        'query_count': len(queries),
        'answer_score': f'{total_score}/{total_max}',
        'answer_pct': round(answer_pct, 1),
        'results': results,
    }
    if ir:
        output['precision_at_k'] = round(ir['precision_at_k'], 3)
        output['recall_at_k'] = round(ir['recall_at_k'], 3)
        output['mrr'] = round(ir['mrr'], 3)

    return output


def compare_chunk_results():
    """Load all chunk-*.json files and print a comparison table."""
    files = sorted(f for f in os.listdir(RESULTS_DIR) if f.startswith('chunk-') and f.endswith('.json'))
    if not files:
        print('No chunk sweep results found in', RESULTS_DIR)
        return

    runs = []
    for f in files:
        with open(os.path.join(RESULTS_DIR, f)) as fh:
            runs.append(json.load(fh))

    print('\n' + '=' * 90)
    print('CHUNK SIZE SWEEP RESULTS')
    print(f'{"Chunk":>7}  {"Answer":>10}  {"Answer%":>8}  {"P@k":>6}  {"R@k":>6}  {"MRR":>6}  {"Latency":>8}')
    print('-' * 90)

    for r in sorted(runs, key=lambda x: x.get('answer_pct', 0), reverse=True):
        avg_lat = sum(res['latency_ms'] for res in r['results']) / len(r['results'])
        marker = ' <-- current' if r['chunk_size'] == 300 else ''
        print(f'{r["chunk_size"]:>7}  {r["answer_score"]:>10}  {r["answer_pct"]:>7.1f}%  '
              f'{r.get("precision_at_k", 0):>6.3f}  '
              f'{r.get("recall_at_k", 0):>6.3f}  '
              f'{r.get("mrr", 0):>6.3f}  '
              f'{avg_lat:>7.0f}ms{marker}')

    print('-' * 90)
    best = max(runs, key=lambda x: x.get('answer_pct', 0))
    print(f'Best: chunk_size={best["chunk_size"]} '
          f'(Answer={best["answer_pct"]:.0f}%, '
          f'R@k={best.get("recall_at_k", 0):.3f}, '
          f'MRR={best.get("mrr", 0):.3f})')


def main():
    parser = argparse.ArgumentParser(description='Chunk size evaluation sweep')
    parser.add_argument('--endpoint', default='http://localhost:8080',
                        help='RAG API base URL')
    parser.add_argument('--chunk-size', type=int, default=None,
                        help='Chunk size being tested (for labelling)')
    parser.add_argument('--compare', action='store_true',
                        help='Compare all chunk sweep results')
    args = parser.parse_args()

    if args.compare:
        compare_chunk_results()
        return

    if args.chunk_size is None:
        print('Error: --chunk-size is required (or use --compare)')
        sys.exit(1)

    queries = load_queries()
    print(f'Chunk sweep: chunk_size={args.chunk_size}, {len(queries)} queries\n')

    output = run_eval(args.endpoint, args.chunk_size, queries)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    output_path = os.path.join(RESULTS_DIR, f'chunk-{args.chunk_size}.json')
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f'\nAnswer: {output["answer_score"]} ({output["answer_pct"]}%)')
    if 'precision_at_k' in output:
        print(f'P@k={output["precision_at_k"]:.3f}  R@k={output["recall_at_k"]:.3f}  MRR={output["mrr"]:.3f}')
    print(f'Results saved to {output_path}')


if __name__ == '__main__':
    main()
