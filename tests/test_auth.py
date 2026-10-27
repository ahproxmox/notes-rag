"""Bearer-token check on the index routes (identity-lab todo 526)."""
import json
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api import auth as auth_mod

ISS = 'https://auth.example/realms/homelab'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    def get_key(self, kid):
        if kid != 'k1':
            raise LookupError(kid)
        return jwt.PyJWK.from_dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())) | {'kid': 'k1'})


def token(key=KEY, kid='k1', alg='RS256', **claims):
    now = int(time.time())
    body = {'iss': ISS, 'aud': 'rag-index', 'iat': now, 'exp': now + 300, 'azp': 'mcp-search',
            'sub': 'svc-1', 'scope': 'rag-index:read'} | claims
    return jwt.encode(body, key, algorithm=alg, headers={'kid': kid})


def bearer(t):
    return {'Authorization': f'Bearer {t}'}


@pytest.fixture
def client_for(monkeypatch):
    """TestClient with the app's authenticator swapped for one in the given mode."""
    import sys
    from api import app  # api/__init__ re-exports the FastAPI instance as `app`
    app_mod = sys.modules['api.app']

    def make(mode):
        a = auth_mod.Authenticator(mode=mode, issuer=ISS, jwks=FakeJWKS())
        monkeypatch.setattr(app_mod._auth, 'mode', a.mode)
        monkeypatch.setattr(app_mod._auth, 'issuer', a.issuer)
        monkeypatch.setattr(app_mod._auth, 'jwks', a.jwks)
        monkeypatch.setattr(app_mod._auth, '_audit_seen', {})
        return TestClient(app)
    return make


RETRIEVE = {'query': 'q', 'k': 3}


def test_classify():
    c = auth_mod.classify
    for p in ['/search', '/api/search', '/search/stream', '/api/search/stream', '/retrieve', '/api/retrieve',
              '/similar', '/api/similar']:
        assert c('POST', p) == 'protected', p
    assert c('GET', '/api/workspace/context/x.md') == 'protected'
    assert c('POST', '/api/notes/search') == 'protected'
    assert c('GET', '/api/notes/recent') == 'protected'
    assert c('GET', '/api/notes/Inbox/a.md') == 'protected'
    assert c('PATCH', '/api/notes/Inbox/a.md') == 'protected'
    assert c('POST', '/api/notes/create') == 'audited'
    assert c('GET', '/api/kanban/todos/526/raw') == 'audited'
    assert c('GET', '/api/todos/next-id') == 'audited'
    assert c('GET', '/health') == 'open'
    assert c('GET', '/api/health') == 'open'
    assert c('GET', '/metrics') == 'open'
    assert c('GET', '/chat') == 'open'
    assert c('GET', '/ui/app.js') == 'open'


def test_enforce_valid_token_passes(client_for):
    c = client_for('enforce')
    with patch('api.app.retrieve', return_value=[{'content': 'c'}]):
        r = c.post('/retrieve', json=RETRIEVE, headers=bearer(token()))
    assert r.status_code == 200
    assert r.json() == {'chunks': [{'content': 'c'}]}


def test_enforce_no_token_401_without_error_code(client_for):
    r = client_for('enforce').post('/api/retrieve', json=RETRIEVE)
    assert r.status_code == 401
    assert r.headers['www-authenticate'] == 'Bearer realm="rag-index"'


@pytest.mark.parametrize('name,tok,reason', [
    ('expired', lambda: token(exp=int(time.time()) - 120), 'invalid or expired token'),
    ('wrong signature', lambda: token(key=OTHER), 'invalid or expired token'),
    ('unknown kid', lambda: token(kid='nope'), 'invalid or expired token'),
    ('wrong issuer', lambda: token(iss='https://evil/realms/homelab'), 'invalid or expired token'),
    ('rag-gateway token (506 audience separation)', lambda: token(aud='rag-gateway', azp='hermes'), 'wrong audience'),
    ('HS256 confusion', lambda: jwt.encode({'iss': ISS, 'aud': 'rag-index', 'exp': int(time.time()) + 60,
                                            'iat': int(time.time())}, 'x' * 32, algorithm='HS256',
                                           headers={'kid': 'k1'}), 'invalid or expired token'),
    ('garbage', lambda: 'not.a.jwt', 'invalid or expired token'),
])
def test_enforce_bad_tokens_401(client_for, name, tok, reason):
    r = client_for('enforce').post('/search', json={'query': 'q'}, headers=bearer(tok()))
    assert r.status_code == 401, name
    assert 'error="invalid_token"' in r.headers['www-authenticate']
    assert r.json()['detail'] == reason


def test_enforce_missing_scope_403(client_for):
    r = client_for('enforce').post('/retrieve', json=RETRIEVE, headers=bearer(token(scope='profile email')))
    assert r.status_code == 403
    assert r.headers['www-authenticate'] == 'Bearer realm="rag-index", error="insufficient_scope", scope="rag-index:read"'


def test_enforce_covers_raw_note_readers(client_for):
    c = client_for('enforce')
    assert c.get('/api/workspace/context/SYSTEM.md').status_code == 401
    assert c.get('/api/notes/recent').status_code == 401
    assert c.post('/api/notes/search', json={'query': 'q'}).status_code == 401
    assert c.post('/api/search/stream', json={'query': 'q'}).status_code == 401


def test_enforce_leaves_kanban_and_health_alone(client_for):
    c = client_for('enforce')
    with patch('api.app.health', return_value={'status': 'ok'}):
        assert c.get('/health').status_code != 401
    r = c.get('/api/todos/next-id')
    assert r.status_code != 401  # audited, never blocked


def test_log_only_allows_and_logs_would_deny(client_for, caplog):
    c = client_for('log-only')
    with patch('api.app.retrieve', return_value=[]), caplog.at_level('WARNING', logger='rag-auth'):
        r = c.post('/retrieve', json=RETRIEVE)
        r2 = c.post('/retrieve', json=RETRIEVE, headers=bearer(token(aud='rag-gateway', azp='hermes')))
    assert r.status_code == 200 and r2.status_code == 200
    events = [json.loads(m.split('[rag-auth] ', 1)[1]) for m in caplog.messages if '[rag-auth]' in m]
    assert events[0]['decision'] == 'would_deny' and events[0]['reason'] == 'no_token'
    assert events[1]['reason'] == 'wrong_audience' and events[1]['azp'] == 'hermes'
    assert events[1]['verified'] is False


def test_allowed_call_logs_identity(client_for, caplog):
    c = client_for('enforce')
    with patch('api.app.retrieve', return_value=[]), caplog.at_level('WARNING', logger='rag-auth'):
        c.post('/retrieve', json=RETRIEVE, headers=bearer(token(azp='trading-rag')))
    e = json.loads(caplog.messages[-1].split('[rag-auth] ', 1)[1])
    assert (e['decision'], e['azp'], e['verified'], e['kind']) == ('allow', 'trading-rag', True, 'protected')


def test_audited_routes_logged_once_per_window(client_for, caplog):
    c = client_for('log-only')
    with caplog.at_level('WARNING', logger='rag-auth'):
        for _ in range(3):
            c.get('/api/todos/next-id')
    audited = [m for m in caplog.messages if '"kind": "audited"' in m]
    assert len(audited) == 1
    assert json.loads(audited[0].split('[rag-auth] ', 1)[1])['path_group'] == '/api/todos'


def test_off_mode_checks_nothing(client_for):
    with patch('api.app.retrieve', return_value=[]):
        assert client_for('off').post('/retrieve', json=RETRIEVE).status_code == 200


def test_bad_mode_fails_loud():
    with pytest.raises(ValueError, match='RAG_AUTH_MODE'):
        auth_mod.Authenticator(mode='enforcing', issuer=ISS, jwks=FakeJWKS())
