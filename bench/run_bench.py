#!/usr/bin/env python3
"""
Run RAG evaluation queries against a configurable endpoint.

Usage:
    python bench/run_bench.py                           # default: http://localhost:8080
    python bench/run_bench.py --endpoint http://192.168.88.71:8080
    python bench/run_bench.py --output bench/results.json
"""

import argparse
import json
import os
import sys
import time
import requests
import yaml

QUERIES_PATH = os.path.join(os.path.dirname(__file__), 'queries.yaml')


def load_queries(path=QUERIES_PATH):
    with open(path) as f:
        data = yaml.safe_load(f)
    return data['queries']


def run_query(endpoint, query_text):
    """Send a search query to the RAG endpoint and return answer + sources + latency."""
    url = f'{endpoint}/search'
    start = time.time()
    try:
        resp = requests.post(
            url,
            json={'query': query_text},
            timeout=30,
        )
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
                        help='Path to save results JSON (default: stdout)')
    args = parser.parse_args()

    queries = load_queries(args.queries)
    results = []

    print(f'Running {len(queries)} queries against {args.endpoint}\n')

    for q in queries:
        qid = q['id']
        query_text = q['query']
        print(f'  [{qid:2d}] {query_text}...', end=' ', flush=True)

        result = run_query(args.endpoint, query_text)
        result['id'] = qid
        result['query'] = query_text
        result['expected'] = q['expected']
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
        'results': results,
    }

    if args.output:
        with open(args.output, 'w') as f:
            json.dump(output, f, indent=2)
        print(f'\nResults saved to {args.output}')
        print(f'Score with: python bench/score.py {args.output}')
    else:
        print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
