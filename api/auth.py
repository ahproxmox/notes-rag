"""Keycloak bearer-token check for the notes index (identity-lab todos 526, 579).

CT 111 is a resource server for audience `rag-index`. Two tiers:

* Protected routes (read the index or the raw notes) always need scope `rag-index:read`.
* Everything else is default-deny: only the static page shells and PWA files in OPEN_PAGES / OPEN_PATHS
  (and the /ui/ asset mount) answer without a token. Every other route belongs to a group with
  its own scope (todo 579):
    reminders  rag-api:reminders   /api/reminders/*
    ingest     rag-api:ingest      /api/ingest*
    todos      rag-api:todos       /api/todos/*, /api/kanban/*
    admin      rag-api:admin       everything else (notes create, queue, review, links, projects,
                                   models, kanban agent turn, stats, ...)
  A group is only enforced when it is listed in RAG_AUTH_ENFORCE_GROUPS and the mode is
  `enforce`; until then its callers are logged (deduplicated) with what the check would say.

Validation follows rag-gateway (identity-lab `rag-gateway/app/auth.py` + `jwks.py`): RS256 only,
cached JWKS with a rate-limited refetch on an unknown kid, iss/aud/exp checked.

RAG_AUTH_MODE:
  off       no checks (default)
  log-only  check and log what would be rejected, allow everything
  enforce   reject protected routes, and routes of the groups in RAG_AUTH_ENFORCE_GROUPS,
            without a valid token (401/403)
RAG_AUTH_ENFORCE_GROUPS: comma-separated group names, empty by default. An unknown name stops
the service at start-up rather than silently leaving a group open.

Rollback: remove a group from RAG_AUTH_ENFORCE_GROUPS (or set RAG_AUTH_MODE=log-only) in the
systemd drop-in /etc/systemd/system/rag.service.d/auth.conf, then `systemctl restart rag`.
"""
import json
import logging
import os
import time

import jwt
import requests
from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger('rag-auth')

MODES = ('off', 'log-only', 'enforce')
ALLOWED_ALGS = ['RS256']

# Routes that read the index or raw notes. Exact paths (any method) ...
PROTECTED_PATHS = {
    '/search', '/search/stream', '/retrieve', '/similar',
    '/api/search', '/api/search/stream', '/api/retrieve', '/api/similar',
}
# ... and prefixes. /api/notes/* covers /api/notes/search, /recent and
# GET/PATCH /api/notes/{filename}; /api/notes/create belongs to the admin group.
PROTECTED_PREFIXES = ('/api/workspace/', '/api/notes/')
UNPROTECTED_EXCEPTIONS = {('POST', '/api/notes/create')}
# Never checked or logged: liveness and Prometheus.
OPEN_PATHS = {'/health', '/api/health', '/metrics'}
# Static page shells and PWA files (no note data in them; the data comes from /api, which is
# checked). Everything not listed here is NOT open: a route nobody classified falls into the
# admin group (default deny), so adding a handler can never silently expose data.
OPEN_PAGES = {'/', '/notes', '/chat', '/new', '/review', '/reports', '/projects', '/kanban',
              '/manifest.json', '/sw.js'}
OPEN_PREFIXES = ('/ui/',)  # static asset mount; '/ui' itself is a redirect

# Route groups for everything else under /api (todo 579): group -> required scope.
GROUP_SCOPES = {
    'reminders': 'rag-api:reminders',
    'ingest': 'rag-api:ingest',
    'todos': 'rag-api:todos',
    'admin': 'rag-api:admin',
}

AUDIT_DEDUP_SECONDS = 600


def classify(method: str, path: str) -> str:
    """'protected' | 'audited' | 'open' for a request. Default deny: only the listed paths are open."""
    if path in OPEN_PATHS or path in OPEN_PAGES or path == '/ui' or path.startswith(OPEN_PREFIXES):
        return 'open'
    if (method, path) in UNPROTECTED_EXCEPTIONS:
        return 'audited'
    if path in PROTECTED_PATHS or path.startswith(PROTECTED_PREFIXES):
        return 'protected'
    return 'audited'  # admin group unless route_group() puts it elsewhere


def route_group(path: str) -> str:
    """Group of an audited route: reminders | ingest | todos | admin."""
    if path.startswith('/api/reminders'):
        return 'reminders'
    if path.startswith('/api/ingest'):
        return 'ingest'
    if path.startswith('/api/kanban/agent/'):  # an agent turn is an action, not a todo read/write
        return 'admin'
    if path.startswith(('/api/todos', '/api/kanban')):
        return 'todos'
    return 'admin'


def _parse_groups(value: str | list[str] | None) -> frozenset[str]:
    if value is None:
        value = os.environ.get('RAG_AUTH_ENFORCE_GROUPS', '')
    names = [g.strip() for g in value.split(',')] if isinstance(value, str) else list(value)
    groups = frozenset(g for g in names if g)
    unknown = groups - GROUP_SCOPES.keys()
    if unknown:
        raise ValueError(f'RAG_AUTH_ENFORCE_GROUPS has unknown group(s) {sorted(unknown)}; '
                         f'known: {sorted(GROUP_SCOPES)}')
    return groups


class JWKSCache:
    def __init__(self, url: str, min_refresh_seconds: int = 60):
        self._url = url
        self._min_refresh = min_refresh_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._last_fetch = 0.0

    def _refresh(self) -> None:
        resp = requests.get(self._url, timeout=5)
        resp.raise_for_status()
        jwks = jwt.PyJWKSet.from_dict(resp.json())
        self._keys = {k.key_id: k for k in jwks.keys if k.public_key_use in (None, 'sig')}
        self._last_fetch = time.monotonic()
        log.info('JWKS refreshed from %s: kids=%s', self._url, sorted(self._keys))

    def get_key(self, kid: str) -> jwt.PyJWK:
        if kid not in self._keys and time.monotonic() - self._last_fetch >= self._min_refresh:
            self._refresh()
        try:
            return self._keys[kid]
        except KeyError:
            raise LookupError(f'unknown kid {kid!r}') from None


def _identity(claims: dict | None, verified: bool) -> dict | None:
    if not claims:
        return None
    return {
        'verified': verified,
        'sub': claims.get('sub'),
        'azp': claims.get('azp'),
        'preferred_username': claims.get('preferred_username'),
        'scope': claims.get('scope'),
    }


class Authenticator:
    def __init__(self, mode: str | None = None, issuer: str | None = None, audience: str | None = None,
                 scope: str | None = None, jwks: JWKSCache | None = None,
                 enforce_groups: str | list[str] | None = None):
        self.mode = mode if mode is not None else os.environ.get('RAG_AUTH_MODE', 'off')
        if self.mode not in MODES:
            raise ValueError(f'RAG_AUTH_MODE must be one of {MODES}, got {self.mode!r}')
        self.issuer = issuer or os.environ.get(
            'RAG_AUTH_ISSUER', 'https://auth.internal.ahproxmox-claude.cc/realms/homelab')
        self.audience = audience or os.environ.get('RAG_AUTH_AUDIENCE', 'rag-index')
        self.scope = scope or os.environ.get('RAG_AUTH_SCOPE', 'rag-index:read')
        self.jwks = jwks or JWKSCache(f'{self.issuer}/protocol/openid-connect/certs')
        self.enforce_groups = _parse_groups(enforce_groups)
        self._audit_seen: dict[tuple, float] = {}

    def verify(self, authorization: str | None, scope: str | None = None) -> tuple[dict | None, str | None]:
        """(identity, reason). reason is None when the token is valid for this API and `scope`."""
        scope = scope or self.scope
        scheme, _, token = (authorization or '').partition(' ')
        if not authorization:
            return None, 'no_token'
        unverified = None
        try:
            unverified = jwt.decode(token, options={'verify_signature': False})
        except jwt.InvalidTokenError:
            pass
        if scheme.lower() != 'bearer' or not token:
            return _identity(unverified, False), 'invalid_token'
        try:
            header = jwt.get_unverified_header(token)
            key = self.jwks.get_key(header.get('kid', ''))
            claims = jwt.decode(token, key=key.key, algorithms=ALLOWED_ALGS, audience=self.audience,
                                issuer=self.issuer, options={'require': ['exp', 'iat', 'iss', 'aud']},
                                leeway=30)
        except jwt.InvalidAudienceError:
            return _identity(unverified, False), 'wrong_audience'
        except (jwt.InvalidTokenError, LookupError):
            return _identity(unverified, False), 'invalid_token'
        except requests.RequestException as e:
            log.error('token validation unavailable: %s', e)
            return _identity(unverified, False), 'auth_unavailable'
        identity = _identity(claims, True)
        if scope not in set((claims.get('scope') or '').split()):
            return identity, 'insufficient_scope'
        return identity, None

    def reject(self, reason: str, scope: str | None = None) -> JSONResponse:
        scope = scope or self.scope
        realm = f'Bearer realm="{self.audience}"'
        if reason == 'no_token':  # RFC 6750 §3.1: no credentials, no error code
            return JSONResponse({'detail': 'missing bearer token'}, 401, headers={'WWW-Authenticate': realm})
        if reason == 'insufficient_scope':
            return JSONResponse({'detail': f'missing scope {scope}'}, 403, headers={
                'WWW-Authenticate': f'{realm}, error="insufficient_scope", scope="{scope}"'})
        if reason == 'auth_unavailable':
            return JSONResponse({'detail': 'token validation unavailable'}, 503, headers={'Retry-After': '30'})
        description = {'wrong_audience': 'wrong audience'}.get(reason, 'invalid or expired token')
        return JSONResponse({'detail': description}, 401, headers={
            'WWW-Authenticate': f'{realm}, error="invalid_token", error_description="{description}"'})

    def _should_log_audited(self, key: tuple) -> bool:
        now = time.monotonic()
        if now - self._audit_seen.get(key, -AUDIT_DEDUP_SECONDS) < AUDIT_DEDUP_SECONDS:
            return False
        self._audit_seen[key] = now
        return True

    async def middleware(self, request: Request, call_next):
        path = request.url.path
        kind = classify(request.method, path)
        if self.mode == 'off' or kind == 'open':
            return await call_next(request)
        group = route_group(path) if kind == 'audited' else None
        scope = GROUP_SCOPES[group] if group else self.scope
        identity, reason = self.verify(request.headers.get('authorization'), scope)
        peer = request.client.host if request.client else None
        # A route is blocked when it is protected, or when its group is listed for enforcement.
        blocking = kind == 'protected' or group in self.enforce_groups
        if reason is None:
            decision = 'allow'
        elif blocking:
            decision = 'deny' if self.mode == 'enforce' else 'would_deny'
        else:  # audited and not enforced: only say what the check would have said
            decision = 'unauthenticated' if reason == 'no_token' else 'would_deny'
        event = {
            'path': path, 'method': request.method, 'kind': kind, 'mode': self.mode,
            'decision': decision, 'reason': reason, 'peer': peer,
            'forwarded_for': request.headers.get('x-forwarded-for'),
            'user_agent': request.headers.get('user-agent'), **(identity or {'verified': None}),
        }
        if group:
            event |= {'group': group, 'required_scope': scope, 'path_group': '/'.join(path.split('/')[:3])}
        deny = reason is not None and blocking and self.mode == 'enforce'
        if kind == 'protected' or deny:
            log.warning('[rag-auth] %s', json.dumps(event))
        elif self._should_log_audited((request.method, event['path_group'], peer, event.get('azp'), reason)):
            # Who calls the rest of the API: once per caller, route group and outcome per 10 min.
            log.warning('[rag-auth] %s', json.dumps(event))
        if deny:
            return self.reject(reason, scope)
        return await call_next(request)
