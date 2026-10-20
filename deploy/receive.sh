#!/usr/bin/env bash
# Server-side half of every deploy. Runs inside CT 111 as root.
#
#   notes-rag-receive <rev>      (or the rev in SSH_ORIGINAL_COMMAND)
#
# scripts/deploy.sh copies this script to /usr/local/sbin/notes-rag-receive; the CT 121 runner's
# key in /root/.ssh/authorized_keys is pinned to that path with command="...", so the runner can
# do nothing on CT 111 except fetch, reset to a validated revision and run scripts/deploy.sh.
# Same pattern as hermes-jobs (CT 128), job-search (CT 142) and proxmox-home (CT 126).
set -euo pipefail

APP_DIR=/opt/rag
REV="${1:-${SSH_ORIGINAL_COMMAND:-}}"
if [[ ! "$REV" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "receive: expected a git revision, got '$REV'" >&2
  exit 2
fi

echo "==> receiving $REV"
cd "$APP_DIR"
git fetch origin
git reset --hard "$REV"
bash scripts/deploy.sh
echo "==> deployed $REV"
