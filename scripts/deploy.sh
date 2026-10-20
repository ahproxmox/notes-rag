#!/bin/bash
set -e
COMMIT=$(git rev-parse --short HEAD)
sed -i "s/const CACHE = \"notes-rag-[^\"]*\"/const CACHE = \"notes-rag-${COMMIT}\"/" ui/sw.js
systemctl restart rag

echo "Waiting for rag service to become healthy..."
for i in $(seq 1 15); do
  if curl -sf http://localhost:8080/api/stats > /dev/null 2>&1; then
    echo "Service healthy"
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "ERROR: rag service failed to become healthy after restart" >&2
    systemctl status rag --no-pager >&2
    exit 1
  fi
  sleep 2
done

if [ -d inbox-viewer ]; then
  sed -i "s/const CACHE = 'reports-[^']*'/const CACHE = 'reports-${COMMIT}'/" inbox-viewer/templates.go
  (cd inbox-viewer && go build -o /opt/inbox-viewer/inbox-viewer .)
  systemctl restart inbox-viewer
fi

echo "Deployed commit ${COMMIT}"
