#!/usr/bin/env python3
"""
Fetch secrets from HashiCorp Vault and write to .env file.
Optional — only used when VAULT_ADDR is set and Vault AppRole credentials exist.

Expects:
  - VAULT_ADDR env var (e.g. http://192.168.88.65:8200)
  - /etc/vault/role-id and /etc/vault/secret-id files
  - Vault secret at secret/data/homelab/openclaw with openrouter_key and brave_key
"""
import json
import os
import urllib.request

VAULT_ADDR = os.environ.get('VAULT_ADDR')
ROLE_ID_PATH = os.environ.get('VAULT_ROLE_ID_PATH', '/etc/vault/role-id')
SECRET_ID_PATH = os.environ.get('VAULT_SECRET_ID_PATH', '/etc/vault/secret-id')
VAULT_SECRET_PATH = os.environ.get('VAULT_SECRET_PATH', 'secret/data/homelab/openclaw')
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')

def main():
    if not VAULT_ADDR:
        print('[vault-fetch] VAULT_ADDR not set, skipping')
        return

    role_id = open(ROLE_ID_PATH).read().strip()
    secret_id = open(SECRET_ID_PATH).read().strip()

    req = urllib.request.Request(
        f'{VAULT_ADDR}/v1/auth/approle/login',
        data=json.dumps({'role_id': role_id, 'secret_id': secret_id}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req) as resp:
        token = json.load(resp)['auth']['client_token']

    req = urllib.request.Request(
        f'{VAULT_ADDR}/v1/{VAULT_SECRET_PATH}',
        headers={'X-Vault-Token': token}
    )
    with urllib.request.urlopen(req) as resp:
        secrets = json.load(resp)['data']['data']

    with open(ENV_PATH, 'w') as f:
        f.write(f'OPENROUTER_API_KEY={secrets["openrouter_key"]}\n')
        f.write(f'BRAVE_API_KEY={secrets["brave_key"]}\n')

    print(f'[vault-fetch] secrets written to {ENV_PATH}')

if __name__ == '__main__':
    main()
