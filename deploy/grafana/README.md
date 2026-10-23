# Grafana RAG dashboard (CT 118)

CI deploys only to CT 111. These files are installed on CT 118 (Prometheus + Grafana, 192.168.88.73) by hand.

| File | Installed as |
|---|---|
| `prometheus-scrape.yml` | appended under `scrape_configs` in `/etc/prometheus/prometheus.yml`, then `promtool check config` + `systemctl reload prometheus` |
| `notes-rag-dashboards.yaml` | `/etc/grafana/provisioning/dashboards/notes-rag.yaml` |
| `notes-rag-dashboard.json` | `/var/lib/grafana/dashboards/notes-rag/notes-rag.json` (re-read every 60 s) |
| `notes-rag-alerts.yaml` | `/etc/grafana/provisioning/alerting/notes-rag.yaml`, then `systemctl restart grafana-server` |

Alerts go to the default notification policy (Discord), like `trading-hermes`.

## Where the data comes from

- **Live:** `GET /metrics` on the rag API (`core/metrics.py`): search endpoint latency, answer-LLM latency/errors/output tokens per model, index size, configured answer model. Scrape job `notes-rag`.
- **Quality:** `bench/scheduled.sh` run by `deploy/rag-bench-{retrieve,answer}.timer` on CT 111 pushes to the Pushgateway (`job=notes_rag_bench`, `kind=retrieve|answer`) via `bench/publish.py`.
- **Indexing:** `core/health.py` already pushes `job=notes_rag` (failures, queue depth).

## Alerts

| Rule | Fires when |
|---|---|
| `notes-rag-recall-drop` | recall@k on `queries-2026-10` < 0.80 |
| `notes-rag-answer-llm-slow` | answer LLM p90 > 20 s for 30 m |
| `notes-rag-retrieve-slow` | `/retrieve` p50 > 2 s for 15 m |
| `notes-rag-bench-stale` | no retrieve push for 36 h, or no answer push for 8 d |
| `notes-rag-down` | `up{job="notes-rag"}` is 0 for 5 m |
