"""Keycloak token for the benches (identity-lab todo 526).

CT 111 requires aud=rag-index tokens on /search and /retrieve. The scheduled
benches call it as client `rag-bench` (client credentials). The secret comes
from Vault via /usr/local/bin/vault-fetch-rag.py, which writes
RAG_BENCH_CLIENT_SECRET into /opt/rag/.env. Without it, benches call with no
token (fine while RAG_AUTH_MODE is off or log-only; they fail with 401 once
enforced, which shows up as a failed bench run).
"""
import os
import sys
import time

import requests

TOKEN_URL = os.environ.get(
    'RAG_AUTH_TOKEN_URL', 'https://auth.internal.ahproxmox-claude.cc/realms/homelab/protocol/openid-connect/token')
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')

_cache = {'token': None, 'exp': 0.0}


def _secret() -> str | None:
    if os.environ.get('RAG_BENCH_CLIENT_SECRET'):
        return os.environ['RAG_BENCH_CLIENT_SECRET']
    try:
        with open(ENV_FILE) as f:
            for line in f:
                k, _, v = line.partition('=')
                if k.strip() == 'RAG_BENCH_CLIENT_SECRET':
                    return v.strip()
    except OSError:
        pass
    return None


def auth_headers() -> dict:
    secret = _secret()
    if not secret:
        print('[bench] RAG_BENCH_CLIENT_SECRET not set: calling without a token', file=sys.stderr)
        return {}
    if _cache['token'] is None or time.time() >= _cache['exp'] - 30:
        resp = requests.post(TOKEN_URL, data={'grant_type': 'client_credentials', 'client_id': 'rag-bench',
                                              'client_secret': secret}, timeout=10)
        resp.raise_for_status()  # fail loud: a bench run without a valid token is a failed run
        body = resp.json()
        _cache.update(token=body['access_token'], exp=time.time() + int(body.get('expires_in', 60)))
    return {'Authorization': f'Bearer {_cache["token"]}'}
