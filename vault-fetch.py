#!/usr/bin/env python3
import json, os, urllib.request

VAULT_ADDR = os.environ.get('VAULT_ADDR')
ROLE_ID_PATH = '/etc/vault/role-id'
SECRET_ID_PATH = '/etc/vault/secret-id'
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')


def _vault_get(token, path):
    req = urllib.request.Request(
        VAULT_ADDR + '/v1/' + path,
        headers={'X-Vault-Token': token},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)['data']['data']


def main():
    if not VAULT_ADDR:
        print('[vault-fetch] VAULT_ADDR not set, skipping')
        return

    role_id = open(ROLE_ID_PATH).read().strip()
    secret_id = open(SECRET_ID_PATH).read().strip()

    req = urllib.request.Request(
        VAULT_ADDR + '/v1/auth/approle/login',
        data=json.dumps({'role_id': role_id, 'secret_id': secret_id}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req) as resp:
        token = json.load(resp)['auth']['client_token']

    openclaw = _vault_get(token, 'secret/data/homelab/openclaw')
    icloud = {}
    try:
        icloud = _vault_get(token, 'secret/data/homelab/icloud')
    except Exception as e:
        print('[vault-fetch] icloud secret unavailable: ' + str(e))

    with open(ENV_PATH, 'w') as f:
        f.write('OPENROUTER_API_KEY=' + openclaw['openrouter_key'] + '\n')
        f.write('BRAVE_API_KEY=' + openclaw['brave_key'] + '\n')
        if icloud.get('apple_id'):
            f.write('CALDAV_APPLE_ID=' + icloud['apple_id'] + '\n')
            f.write('CALDAV_APP_PASSWORD=' + icloud['app_password'] + '\n')

    print('[vault-fetch] secrets written to ' + ENV_PATH)


if __name__ == '__main__':
    main()
