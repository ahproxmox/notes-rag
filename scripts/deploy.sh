#!/bin/bash
set -e
COMMIT=$(git rev-parse --short HEAD)
sed -i "s/const CACHE = \"notes-rag-[^\"]*\"/const CACHE = \"notes-rag-${COMMIT}\"/" ui/sw.js
systemctl restart rag
echo "Deployed commit ${COMMIT}"
