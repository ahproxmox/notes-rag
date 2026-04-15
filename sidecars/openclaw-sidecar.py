#!/usr/bin/env python3
"""OpenClaw model reload sidecar — updates openclaw.json and runs pm2 restart."""
import json
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS_CONFIG   = '/mnt/Claude/config/models.json'
OPENCLAW_CONFIG = '/root/.openclaw/openclaw.json'
HEALTH_CONFIG   = '/mnt/Claude/config/model-health.json'
PORT            = 8092


def _or_prefix(model):
    """Add openrouter/ prefix if not already present."""
    return model if model.startswith('openrouter/') else f'openrouter/{model}'


def update_openclaw_models(service):
    with open(MODELS_CONFIG) as f:
        cfg = json.load(f)

    with open(OPENCLAW_CONFIG) as f:
        oc = json.load(f)

    changed = False

    if service in ('openclaw', None):
        model = cfg.get('openclaw', '').strip()
        if model:
            oc['agents']['defaults']['model']['primary'] = _or_prefix(model)
            changed = True

    if not changed:
        raise ValueError(f'Nothing updated for service: {service}')

    with open(OPENCLAW_CONFIG, 'w') as f:
        json.dump(oc, f, indent=2)

    subprocess.run(['pm2', 'restart', 'openclaw'], check=True, timeout=60)
    return service


def write_health(service, status, error=None):
    try:
        with open(HEALTH_CONFIG) as f:
            health = json.load(f)
    except Exception:
        health = {}
    entry = {'status': status, 'last_checked': datetime.now(timezone.utc).isoformat()}
    if error:
        entry['error'] = str(error)[:200]
    health[service] = entry
    with open(HEALTH_CONFIG, 'w') as f:
        json.dump(health, f, indent=2)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/reload':
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            service = body.get('service', 'openclaw')
            update_openclaw_models(service)
            write_health(service, 'ok')
            self._respond(200, {'ok': True, 'service': service})
        except Exception as e:
            service = 'openclaw'
            write_health(service, 'error', error=e)
            self._respond(500, {'ok': False, 'error': str(e)})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[openclaw-sidecar] listening on :{PORT}', flush=True)
    server.serve_forever()
