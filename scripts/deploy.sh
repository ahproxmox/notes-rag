#!/bin/bash
set -e
COMMIT=$(git rev-parse --short HEAD)
sed -i "s/const CACHE = \"notes-rag-[^\"]*\"/const CACHE = \"notes-rag-${COMMIT}\"/" ui/sw.js
systemctl restart rag

if [ -d inbox-viewer ]; then
  sed -i "s/const CACHE = 'reports-[^']*'/const CACHE = 'reports-${COMMIT}'/" inbox-viewer/templates.go
  (cd inbox-viewer && go build -o /opt/inbox-viewer/inbox-viewer .)
  systemctl restart inbox-viewer
fi

echo "Deployed commit ${COMMIT}"
