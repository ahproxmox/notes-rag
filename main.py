import os
import sys
import subprocess
import threading
import time


def load_env(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()


def run_vault_fetch():
    vault_fetch = os.path.join(os.path.dirname(__file__), 'vault-fetch.py')
    if not os.path.exists(vault_fetch):
        print('[main] vault-fetch.py not found, skipping Vault', flush=True)
        return
    if not os.environ.get('VAULT_ADDR'):
        print('[main] VAULT_ADDR not set, skipping Vault', flush=True)
        return
    print('[main] fetching secrets from Vault...', flush=True)
    result = subprocess.run(
        [sys.executable, vault_fetch],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'[main] vault-fetch failed: {result.stderr}', flush=True)
        sys.exit(1)
    print(result.stdout.strip(), flush=True)


def init_fts_index():
    """Initialise FTS5 index — populate from ChromaDB if empty."""
    from indexer import load_config, get_embeddings, get_db
    from fts import FTSIndex
    from search import init_fts

    cfg = load_config()
    fts_path = os.path.join(os.path.dirname(cfg['chroma_path']), 'fts.db')
    fts = FTSIndex(fts_path)
    init_fts(fts)

    if fts.count() == 0:
        print('[main] FTS5 index empty — populating from ChromaDB...', flush=True)
        embeddings = get_embeddings(cfg)
        db = get_db(cfg, embeddings)
        fts.rebuild_from_chroma(db)
        print(f'[main] FTS5 ready: {fts.count()} chunks', flush=True)
    else:
        print(f'[main] FTS5 ready: {fts.count()} chunks', flush=True)


def run_watcher():
    from watcher import start_watcher
    start_watcher()


def run_api():
    import uvicorn
    port = int(os.environ.get('RAG_PORT', 8080))
    uvicorn.run('api:app', host='0.0.0.0', port=port, log_level='warning')


if __name__ == '__main__':
    run_vault_fetch()
    load_env()

    print('[main] initialising FTS5 index...', flush=True)
    init_fts_index()

    print('[main] starting watcher...', flush=True)
    t = threading.Thread(target=run_watcher, daemon=True)
    t.start()

    time.sleep(2)
    port = os.environ.get('RAG_PORT', 8080)
    print(f'[main] starting API on :{port}', flush=True)
    run_api()
