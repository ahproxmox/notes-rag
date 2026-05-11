#!/usr/bin/env python3
"""
Reconcile Paperless documents against the RAG index.
Runs every 30 min via cron. Ingests any document not yet in rag.db.
"""
import json, os, sqlite3, sys, urllib.request
from pathlib import Path


def _load_env():
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

PAPERLESS_URL   = os.environ.get('PAPERLESS_URL',   'http://192.168.88.85:8000')
PAPERLESS_TOKEN = os.environ.get('PAPERLESS_TOKEN')
RAG_URL         = os.environ.get('RAG_URL',         'http://192.168.88.71:8080')
RAG_DB          = os.environ.get('RAG_DB',          '/opt/rag/rag.db')

if not PAPERLESS_TOKEN:
    print('[paperless-reconcile] PAPERLESS_TOKEN env var required', file=sys.stderr)
    sys.exit(1)


def paperless_get(path):
    req = urllib.request.Request(
        f'{PAPERLESS_URL}{path}',
        headers={'Authorization': f'Token {PAPERLESS_TOKEN}'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def rag_ingest(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'{RAG_URL}/api/ingest/paperless',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def indexed_ids():
    conn = sqlite3.connect(RAG_DB)
    rows = conn.execute(
        "SELECT DISTINCT source FROM chunks WHERE source LIKE 'paperless:%'"
    ).fetchall()
    conn.close()
    return {int(r[0].split(':')[1]) for r in rows}


def all_paperless_ids():
    ids, page = [], 1
    while True:
        data = paperless_get(f'/api/documents/?page={page}&page_size=100&fields=id')
        ids.extend(d['id'] for d in data['results'])
        if not data.get('next'):
            break
        page += 1
    return set(ids)


def ingest_doc(doc_id):
    doc = paperless_get(f'/api/documents/{doc_id}/')
    if not doc.get('content', '').strip():
        print(f'  skip {doc_id} "{doc["title"]}" -- no content (OCR pending?)')
        return False
    payload = {
        'paperless_id': doc['id'],
        'title':        doc.get('title') or '',
        'content':      doc.get('content') or '',
        'correspondent': str(doc['correspondent']) if doc.get('correspondent') is not None else '',
        'doc_type':     str(doc['document_type']) if doc.get('document_type') is not None else '',
        'tags':         [str(t) for t in (doc.get('tags') or [])],
        'created':      doc.get('created') or '',
    }
    result = rag_ingest(payload)
    print(f'  ingested {doc_id} "{doc["title"]}" -- {result.get("chunks_stored")} chunks')
    return True


def main():
    try:
        already = indexed_ids()
        all_ids = all_paperless_ids()
        missing = all_ids - already
        if not missing:
            return
        print(f'[paperless-reconcile] {len(missing)} missing: {sorted(missing)}')
        for doc_id in sorted(missing):
            try:
                ingest_doc(doc_id)
            except Exception as e:
                print(f'  error on {doc_id}: {e}', file=sys.stderr)
    except Exception as e:
        print(f'[paperless-reconcile] fatal: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
