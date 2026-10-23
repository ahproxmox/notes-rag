#!/bin/bash
# Scheduled bench run + Pushgateway publish, for the Grafana RAG dashboard.
#
#   bench/scheduled.sh retrieve   # nightly: retrieve_bench.py on both query sets (no LLM)
#   bench/scheduled.sh answer     # weekly:  run_bench.py through /search (43 answer-LLM calls)
#
# Run by deploy/rag-bench-{retrieve,answer}.timer. Results land in bench/results/
# (gitignored); the newest 30 per kind are kept.
set -euo pipefail
cd "$(dirname "$0")/.."

KIND="${1:?usage: bench/scheduled.sh retrieve|answer}"
PY=/opt/rag/venv/bin/python
OUT="bench/results/scheduled-${KIND}-$(date -u +%Y%m%dT%H%M%SZ).json"
mkdir -p bench/results

case "$KIND" in
  retrieve)
    "$PY" bench/retrieve_bench.py --queries bench/queries.yaml --queries bench/queries-2026-10.yaml \
      --output "$OUT"
    ;;
  answer)
    "$PY" bench/run_bench.py --output "$OUT"
    ;;
  *)
    echo "unknown kind: $KIND" >&2
    exit 2
    ;;
esac

"$PY" bench/publish.py "$KIND" "$OUT"

ls -1t bench/results/scheduled-"${KIND}"-*.json | tail -n +31 | xargs -r rm --
