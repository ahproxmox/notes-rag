#!/usr/bin/env python3
"""
Run RAG evaluation queries against a configurable endpoint.

Usage:
    python bench/run_bench.py                           # default: http://localhost:8080
    python bench/run_bench.py --endpoint http://192.168.1.100:8080
    python bench/run_bench.py --output bench/results/baseline.json
    python bench/run_bench.py --bm25-weight 0.6 --vector-weight 0.4
"""

import argparse
import json
import os
import time
import requests
import yaml

QUERIES_PATH = os.path.join(os.path.dirname(__file__), 'queries.yaml')


def load_queries(path=QUERIES_PATH):
    with open(path) as f:
        data = yaml.safe_load(f)
    return data['queries']


def run_query(endpoint, query_text, bm25_weight=None, vector_weight=None):
    """Send a search query to the RAG endpoint and return answer + sources + latency."""
    url = f'{endpoint}/search'
    body = {'query': query_text}
    if bm25_weight is not None and vector_weight is not None:
        body['bm25_weight'] = bm25_weight
        body['vector_weight'] = vector_weight
    start = time.time()
    try:
        resp = requests.post(url, json=body, timeout=30)
        latency_ms = (time.time() - start) * 1000
        resp.raise_for_status()
        data = resp.json()
        return {
            'answer': data.get('answer', ''),
            'sources': data.get('sources', []),
            'latency_ms': round(latency_ms, 1),
            'error': None,
        }
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        return {
            'answer': '',
            'sources': [],
            'latency_ms': round(latency_ms, 1),
            'error': str(e),
        }


def main():
    parser = argparse.ArgumentParser(description='Run RAG evaluation queries')
    parser.add_argument('--endpoint', default='http://localhost:8080',
                        help='RAG API base URL (default: http://localhost:8080)')
    parser.add_argument('--queries', default=QUERIES_PATH,
                        help='Path to queries YAML file')
    parser.add_argument('--output', default=None,
                        help='Path to save results JSON')
    parser.add_argument('--bm25-weight', type=float, default=None,
                        help='BM25 weight override (requires --vector-weight)')
    parser.add_argument('--vector-weight', type=float, default=None,
                        help='Vector weight override (requires --bm25-weight)')
    args = parser.parse_args()

    queries = load_queries(args.queries)
    results = []

    weights_label = ''
    if args.bm25_weight is not None and args.vector_weight is not None:
        weights_label = f' [BM25={args.bm25_weight}, Vector={args.vector_weight}]'

    print(f'Running {len(queries)} queries against {args.endpoint}{weights_label}\n')

    for q in queries:
        qid = q['id']
        query_text = q['query']
        print(f'  [{qid:2d}] {query_text}...', end=' ', flush=True)

        result = run_query(args.endpoint, query_text,
                           bm25_weight=args.bm25_weight,
                           vector_weight=args.vector_weight)
        result['id'] = qid
        result['query'] = query_text
        result['category'] = q.get('category', '')
        result['expected'] = q['expected']
        result['expected_sources'] = q.get('expected_sources', [])
        result['max_score'] = q['max_score']
        result['scoring'] = q['scoring']
        results.append(result)

        if result['error']:
            print(f'ERROR ({result["latency_ms"]}ms)')
        else:
            print(f'OK ({result["latency_ms"]}ms)')

    output = {
        'endpoint': args.endpoint,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'query_count': len(queries),
        'results': results,
    }
    if args.bm25_weight is not None:
        output['bm25_weight'] = args.bm25_weight
        output['vector_weight'] = args.vector_weight

    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)
        print(f'\nResults saved to {args.output}')
        print(f'Score with: python bench/score.py {args.output}')
    else:
        print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
