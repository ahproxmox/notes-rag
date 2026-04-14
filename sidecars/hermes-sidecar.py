#!/usr/bin/env python3
"""Hermes model reload sidecar — updates config.yaml and restarts hermes-gateway."""
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS_CONFIG  = '/mnt/Claude/config/models.json'
HERMES_CONFIG  = '/root/.hermes/config.yaml'
HEALTH_CONFIG  = '/mnt/Claude/config/model-health.json'
PORT           = 8090


def update_hermes_model():
    with open(MODELS_CONFIG) as f:
        config = json.load(f)
    model = config.get('hermes', '').strip()
    if not model:
        raise ValueError('No hermes model in config')

    with open(HERMES_CONFIG) as f:
        content = f.read()

    # Replace:  default: "old-model"
    updated = re.sub(
        r'(^\s+default:\s+")([^"]+)(")',
        lambda m: m.group(1) + model + m.group(3),
        content,
        flags=re.MULTILINE,
    )
    if updated == content:
        raise ValueError('Pattern not found in config.yaml — check format')

    with open(HERMES_CONFIG, 'w') as f:
        f.write(updated)

    env = {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/0', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/0/bus'}
    subprocess.run(['systemctl', '--user', 'restart', 'hermes-gateway'], env=env, check=True, timeout=60)
    return model


def write_health(status, error=None):
    try:
        with open(HEALTH_CONFIG) as f:
            health = json.load(f)
    except Exception:
        health = {}
    from datetime import datetime, timezone
    entry = {'status': status, 'last_checked': datetime.now(timezone.utc).isoformat()}
    if error:
        entry['error'] = str(error)[:200]
    health['hermes'] = entry
    with open(HEALTH_CONFIG, 'w') as f:
        json.dump(health, f, indent=2)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/reload':
            self.send_response(404)
            self.end_headers()
            return
        try:
            model = update_hermes_model()
            write_health('ok')
            self._respond(200, {'ok': True, 'model': model})
        except Exception as e:
            write_health('error', error=e)
            self._respond(500, {'ok': False, 'error': str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silent


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[hermes-sidecar] listening on :{PORT}', flush=True)
    server.serve_forever()
