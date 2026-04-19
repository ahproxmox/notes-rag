#!/usr/bin/env python3
"""
Fetch secrets from HashiCorp Vault and write to .env file.
"""
import json
import os
import urllib.request

VAULT_ADDR = os.environ.get("VAULT_ADDR")
ROLE_ID_PATH = os.environ.get("VAULT_ROLE_ID_PATH", "/etc/vault/role-id")
SECRET_ID_PATH = os.environ.get("VAULT_SECRET_ID_PATH", "/etc/vault/secret-id")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def _vault_get(token, path):
    req = urllib.request.Request(
        f"{VAULT_ADDR}/v1/{path}",
        headers={"X-Vault-Token": token},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["data"]["data"]


def main():
    if not VAULT_ADDR:
        print("[vault-fetch] VAULT_ADDR not set, skipping")
        return

    role_id = open(ROLE_ID_PATH).read().strip()
    secret_id = open(SECRET_ID_PATH).read().strip()

    req = urllib.request.Request(
        f"{VAULT_ADDR}/v1/auth/approle/login",
        data=json.dumps({"role_id": role_id, "secret_id": secret_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        token = json.load(resp)["auth"]["client_token"]

    openclaw = _vault_get(token, "secret/data/homelab/openclaw")
    icloud = {}
    try:
        icloud = _vault_get(token, "secret/data/homelab/icloud")
    except Exception as e:
        print(f"[vault-fetch] icloud secret unavailable: {e}")

    with open(ENV_PATH, "w") as f:
        f.write(fOPENROUTER_API_KEY={openclaw[openrouter_key]}n)
        f.write(fBRAVE_API_KEY={openclaw[brave_key]}n)
        if icloud.get("apple_id"):
            f.write(fCALDAV_APPLE_ID={icloud[apple_id]}n)
            f.write(fCALDAV_APP_PASSWORD={icloud[app_password]}n)

    print(f"[vault-fetch] secrets written to {ENV_PATH}")


if __name__ == "__main__":
    main()
