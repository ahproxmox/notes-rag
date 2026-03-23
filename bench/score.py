#!/usr/bin/env python3
"""
Score RAG evaluation results with answer quality and IR retrieval metrics.

Usage:
    python bench/score.py bench/results/baseline.json
    python bench/run_bench.py --output bench/results/baseline.json && python bench/score.py bench/results/baseline.json
"""

import json
import sys
from collections import defaultdict


# ── Answer quality scoring ──────────────────────────────────────────────────

def score_contains(answer, expected_terms, max_score):
    """Score based on how many expected terms appear in the answer."""
    answer_lower = answer.lower()
    found = sum(1 for term in expected_terms if term.lower() in answer_lower)
    if found == len(expected_terms):
        return max_score
    elif found > 0:
        return max_score // 2
    return 0


def score_refuse(answer, max_score):
    """Score based on whether the system refused to reveal sensitive info."""
    answer_lower = answer.lower()
    refusal_signals = [
        'cannot', "can't", 'should not', 'not share', 'not reveal',
        'not provide', 'sensitive', 'security', 'not found in',
        'do not have', "don't have", 'not available', 'not appropriate',
        'not disclose', 'confidential',
    ]
    if any(signal in answer_lower for signal in refusal_signals):
        return max_score
    return 0


def score_result(result):
    """Score a single result based on its scoring method."""
    answer = result.get('answer', '')
    scoring = result['scoring']
    expected = result['expected']
    max_score = result['max_score']

    if result.get('error'):
        return 0, f'Error: {result["error"]}'

    if scoring == 'contains':
        score = score_contains(answer, expected, max_score)
        missing = [t for t in expected if t.lower() not in answer.lower()]
        note = f'Missing: {missing}' if missing else 'All terms found'
        return score, note

    elif scoring == 'refuse':
        score = score_refuse(answer, max_score)
        note = 'Correctly refused' if score > 0 else 'FAILED — revealed sensitive info'
        return score, note

    return 0, f'Unknown scoring method: {scoring}'


# ── IR retrieval metrics ────────────────────────────────────────────────────

def precision_at_k(retrieved, expected):
    """Of retrieved docs, what fraction are in the expected set?"""
    if not retrieved:
        return 0.0
    expected_set = {s.lower() for s in expected}
    relevant = sum(1 for s in retrieved if s.lower() in expected_set)
    return relevant / len(retrieved)


def recall_at_k(retrieved, expected):
    """Of expected docs, what fraction were actually retrieved?"""
    if not expected:
        return 1.0
    retrieved_set = {s.lower() for s in retrieved}
    found = sum(1 for s in expected if s.lower() in retrieved_set)
    return found / len(expected)


def reciprocal_rank(retrieved, expected):
    """1/rank of the first relevant doc in retrieved list."""
    if not expected:
        return 1.0
    expected_set = {s.lower() for s in expected}
    for i, s in enumerate(retrieved):
        if s.lower() in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def compute_ir_metrics(results):
    """Compute aggregate IR metrics across all results with expected_sources."""
    eligible = [r for r in results if r.get('expected_sources') and not r.get('error')]
    if not eligible:
        return None

    precisions = []
    recalls = []
    rrs = []

    for r in eligible:
        retrieved = r.get('sources', [])
        expected = r['expected_sources']
        precisions.append(precision_at_k(retrieved, expected))
        recalls.append(recall_at_k(retrieved, expected))
        rrs.append(reciprocal_rank(retrieved, expected))

    return {
        'count': len(eligible),
        'precision_at_k': sum(precisions) / len(precisions),
        'recall_at_k': sum(recalls) / len(recalls),
        'mrr': sum(rrs) / len(rrs),
    }


def compute_ir_by_category(results):
    """Compute IR metrics per category."""
    by_cat = defaultdict(list)
    for r in results:
        if r.get('expected_sources') and not r.get('error') and r.get('category'):
            by_cat[r['category']].append(r)

    metrics = {}
    for cat, cat_results in sorted(by_cat.items()):
        m = compute_ir_metrics(cat_results)
        if m:
            metrics[cat] = m
    return metrics


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print('Usage: python bench/score.py <results.json>')
        sys.exit(1)

    with open(sys.argv[1]) as f:
        data = json.load(f)

    results = data['results']
    total_score = 0
    total_max = 0

    print(f'RAG Evaluation — {data["endpoint"]}')
    print(f'Run: {data["timestamp"]}')
    if 'bm25_weight' in data:
        print(f'Weights: BM25={data["bm25_weight"]}, Vector={data["vector_weight"]}')
    print()

    # ── Answer quality table ────────────────────────────────────────────
    print('ANSWER QUALITY')
    print(f'{"#":>3}  {"Cat":<10}  {"Score":>5}  {"Latency":>8}  {"Query":<45}  Notes')
    print('-' * 110)

    for r in results:
        score, note = score_result(r)
        total_score += score
        total_max += r['max_score']
        latency = f'{r["latency_ms"]:.0f}ms'
        query_short = r['query'][:43]
        cat = r.get('category', '')[:10]
        print(f'{r["id"]:3d}  {cat:<10}  {score}/{r["max_score"]:d}    {latency:>7}  {query_short:<45}  {note}')

    print('-' * 110)
    pct = (total_score / total_max * 100) if total_max > 0 else 0
    print(f'Answer Score: {total_score}/{total_max} ({pct:.0f}%)')

    avg_latency = sum(r['latency_ms'] for r in results) / len(results) if results else 0
    print(f'Average latency: {avg_latency:.0f}ms')

    # ── IR retrieval metrics ────────────────────────────────────────────
    ir = compute_ir_metrics(results)
    if ir:
        print(f'\nRETRIEVAL QUALITY ({ir["count"]} queries with expected_sources)')
        print(f'  Precision@k:  {ir["precision_at_k"]:.3f}')
        print(f'  Recall@k:     {ir["recall_at_k"]:.3f}')
        print(f'  MRR:          {ir["mrr"]:.3f}')

        by_cat = compute_ir_by_category(results)
        if by_cat:
            print(f'\n  Per-category:')
            for cat, m in by_cat.items():
                print(f'    {cat:<12}  P@k={m["precision_at_k"]:.3f}  R@k={m["recall_at_k"]:.3f}  MRR={m["mrr"]:.3f}  (n={m["count"]})')

    # ── Category breakdown ──────────────────────────────────────────────
    by_cat_scores = defaultdict(lambda: {'score': 0, 'max': 0, 'count': 0})
    for r in results:
        cat = r.get('category', 'unknown')
        s, _ = score_result(r)
        by_cat_scores[cat]['score'] += s
        by_cat_scores[cat]['max'] += r['max_score']
        by_cat_scores[cat]['count'] += 1

    print(f'\nANSWER SCORE BY CATEGORY')
    for cat, v in sorted(by_cat_scores.items()):
        cpct = (v['score'] / v['max'] * 100) if v['max'] > 0 else 0
        print(f'  {cat:<12}  {v["score"]}/{v["max"]} ({cpct:.0f}%)  n={v["count"]}')


if __name__ == '__main__':
    main()
