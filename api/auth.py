"""Keycloak bearer-token check for the notes index (identity-lab todo 526).

CT 111 is a resource server for audience `rag-index`: every route that reads
the index or the raw notes needs an access token from realm `homelab` with
scope `rag-index:read`. Validation follows rag-gateway (identity-lab
`rag-gateway/app/auth.py` + `jwks.py`): RS256 only, cached JWKS with a
rate-limited refetch on an unknown kid, iss/aud/exp checked.

RAG_AUTH_MODE:
  off       no checks (default)
  log-only  check and log what would be rejected, allow everything
  enforce   reject protected routes without a valid token (401/403)
Other /api routes are never blocked; their callers are logged (deduplicated)
so the next lockdown knows who needs a token.

Rollback from enforce: RAG_AUTH_MODE=log-only in the systemd drop-in
/etc/systemd/system/rag.service.d/auth.conf, then `systemctl restart rag`.
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
# GET/PATCH /api/notes/{filename}; /api/notes/create stays audited only.
PROTECTED_PREFIXES = ('/api/workspace/', '/api/notes/')
UNPROTECTED_EXCEPTIONS = {('POST', '/api/notes/create')}
# Never checked or logged: liveness and Prometheus.
OPEN_PATHS = {'/health', '/api/health', '/metrics'}

AUDIT_DEDUP_SECONDS = 600


def classify(method: str, path: str) -> str:
    """'protected' | 'audited' | 'open' for a request."""
    if path in OPEN_PATHS:
        return 'open'
    if (method, path) in UNPROTECTED_EXCEPTIONS:
        return 'audited'
    if path in PROTECTED_PATHS or path.startswith(PROTECTED_PREFIXES):
        return 'protected'
    if path.startswith('/api/') or path in {'/stats', '/research', '/log/recent', '/entities'} \
            or path.startswith('/review/'):
        return 'audited'
    return 'open'  # UI pages and static files


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
                 scope: str | None = None, jwks: JWKSCache | None = None):
        self.mode = mode if mode is not None else os.environ.get('RAG_AUTH_MODE', 'off')
        if self.mode not in MODES:
            raise ValueError(f'RAG_AUTH_MODE must be one of {MODES}, got {self.mode!r}')
        self.issuer = issuer or os.environ.get(
            'RAG_AUTH_ISSUER', 'https://auth.internal.ahproxmox-claude.cc/realms/homelab')
        self.audience = audience or os.environ.get('RAG_AUTH_AUDIENCE', 'rag-index')
        self.scope = scope or os.environ.get('RAG_AUTH_SCOPE', 'rag-index:read')
        self.jwks = jwks or JWKSCache(f'{self.issuer}/protocol/openid-connect/certs')
        self._audit_seen: dict[tuple, float] = {}

    def verify(self, authorization: str | None) -> tuple[dict | None, str | None]:
        """(identity, reason). reason is None when the token is valid for this API."""
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
        if self.scope not in set((claims.get('scope') or '').split()):
            return identity, 'insufficient_scope'
        return identity, None

    def reject(self, reason: str) -> JSONResponse:
        realm = f'Bearer realm="{self.audience}"'
        if reason == 'no_token':  # RFC 6750 §3.1: no credentials, no error code
            return JSONResponse({'detail': 'missing bearer token'}, 401, headers={'WWW-Authenticate': realm})
        if reason == 'insufficient_scope':
            return JSONResponse({'detail': f'missing scope {self.scope}'}, 403, headers={
                'WWW-Authenticate': f'{realm}, error="insufficient_scope", scope="{self.scope}"'})
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
        kind = classify(request.method, request.url.path)
        if self.mode == 'off' or kind == 'open':
            return await call_next(request)
        identity, reason = self.verify(request.headers.get('authorization'))
        peer = request.client.host if request.client else None
        event = {
            'path': request.url.path, 'method': request.method, 'kind': kind, 'mode': self.mode,
            'decision': 'allow' if reason is None else ('deny' if self.mode == 'enforce' and kind == 'protected'
                                                         else 'would_deny'),
            'reason': reason, 'peer': peer, 'forwarded_for': request.headers.get('x-forwarded-for'),
            'user_agent': request.headers.get('user-agent'), **(identity or {'verified': None}),
        }
        if kind == 'audited':
            # Who calls the rest of the API: once per caller and route group per 10 min.
            group = '/'.join(request.url.path.split('/')[:3])
            if self._should_log_audited((request.method, group, peer, event.get('azp'))):
                log.warning('[rag-auth] %s', json.dumps({**event, 'path_group': group}))
            return await call_next(request)
        log.warning('[rag-auth] %s', json.dumps(event))
        if reason is not None and self.mode == 'enforce':
            return self.reject(reason)
        return await call_next(request)
