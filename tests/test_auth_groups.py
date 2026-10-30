"""Per-group scopes for the non-index routes (identity-lab todo 579, api/auth.py)."""
import json
import sys

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_mod
from tests.test_auth import FakeJWKS, ISS, bearer, token


@pytest.fixture
def client_for(monkeypatch):
    """TestClient with the app's authenticator in the given mode and enforced groups."""
    from api import app  # api/__init__ re-exports the FastAPI instance as `app`
    app_mod = sys.modules['api.app']

    def make(mode, groups=''):
        a = auth_mod.Authenticator(mode=mode, issuer=ISS, jwks=FakeJWKS(), enforce_groups=groups)
        monkeypatch.setattr(app_mod._auth, 'mode', a.mode)
        monkeypatch.setattr(app_mod._auth, 'issuer', a.issuer)
        monkeypatch.setattr(app_mod._auth, 'jwks', a.jwks)
        monkeypatch.setattr(app_mod._auth, 'enforce_groups', a.enforce_groups)
        monkeypatch.setattr(app_mod._auth, '_audit_seen', {})
        return TestClient(app)
    return make


@pytest.mark.parametrize('path,group', [
    ('/api/reminders/queue', 'reminders'),
    ('/api/reminders/ack', 'reminders'),
    ('/api/ingest', 'ingest'),
    ('/api/ingest/paperless', 'ingest'),
    ('/api/todos/next-id', 'todos'),
    ('/api/todos/create', 'todos'),
    ('/api/kanban/todos/579/raw', 'todos'),
    ('/api/kanban/swimlanes', 'todos'),
    ('/api/kanban/agent/turn', 'admin'),
    ('/api/notes/create', 'admin'),
    ('/api/queue/stash', 'admin'),
    ('/api/review/queue', 'admin'),
    ('/api/links/scan', 'admin'),
    ('/api/projects', 'admin'),
    ('/api/models/validate', 'admin'),
    ('/api/stats', 'admin'),
    ('/review/queue', 'admin'),
])
def test_route_group(path, group):
    assert auth_mod.route_group(path) == group


def test_every_audited_route_has_a_scope():
    for p in ['/api/anything/new', '/stats', '/research', '/log/recent', '/entities', '/review/x',
              '/docs', '/redoc', '/openapi.json']:
        assert auth_mod.classify('GET', p) == 'audited'
        assert auth_mod.GROUP_SCOPES[auth_mod.route_group(p)]


def test_unknown_group_fails_loud():
    with pytest.raises(ValueError, match='unknown group'):
        auth_mod.Authenticator(mode='enforce', enforce_groups='todos,tods')


def test_groups_from_environment(monkeypatch):
    monkeypatch.setenv('RAG_AUTH_ENFORCE_GROUPS', ' reminders , ingest ,')
    assert auth_mod.Authenticator(mode='enforce').enforce_groups == {'reminders', 'ingest'}


def test_enforced_group_rejects_without_token(client_for):
    c = client_for('enforce', 'reminders')
    r = c.get('/api/reminders/queue')
    assert r.status_code == 401
    assert r.headers['www-authenticate'] == 'Bearer realm="rag-index"'


def test_enforced_group_wrong_scope_403_names_the_scope(client_for):
    c = client_for('enforce', 'reminders')
    r = c.get('/api/reminders/queue', headers=bearer(token(scope='rag-index:read rag-api:todos')))
    assert r.status_code == 403
    assert 'scope="rag-api:reminders"' in r.headers['www-authenticate']


def test_enforced_group_accepts_its_scope(client_for):
    c = client_for('enforce', 'todos')
    r = c.get('/api/todos/next-id', headers=bearer(token(scope='rag-api:todos', azp='mac-todos')))
    assert r.status_code == 200


def test_enforced_group_rejects_bad_tokens(client_for):
    c = client_for('enforce', 'todos')
    assert c.get('/api/todos/next-id', headers=bearer('x.y.z')).status_code == 401
    assert c.get('/api/todos/next-id', headers=bearer(token(aud='rag-gateway', scope='rag-api:todos'))).status_code == 401
    assert c.get('/api/todos/next-id', headers=bearer(token(exp=1, scope='rag-api:todos'))).status_code == 401


def test_other_groups_stay_open_when_one_is_enforced(client_for):
    c = client_for('enforce', 'reminders')
    assert c.get('/api/todos/next-id').status_code == 200


def test_log_only_never_blocks_listed_groups(client_for, caplog):
    c = client_for('log-only', 'todos')
    with caplog.at_level('WARNING', logger='rag-auth'):
        assert c.get('/api/todos/next-id').status_code == 200
    e = json.loads(caplog.messages[-1].split('[rag-auth] ', 1)[1])
    assert e['group'] == 'todos' and e['decision'] == 'would_deny'  # listed group: it would be blocked


def test_unenforced_group_logs_what_the_check_would_say(client_for, caplog):
    c = client_for('enforce', '')
    with caplog.at_level('WARNING', logger='rag-auth'):
        c.get('/api/todos/next-id', headers=bearer(token(scope='rag-index:read', azp='lab-cli')))
        c.get('/api/todos/next-id', headers=bearer(token(scope='rag-api:todos', azp='lab-cli')))
    events = [json.loads(m.split('[rag-auth] ', 1)[1]) for m in caplog.messages if '[rag-auth] ' in m]
    assert [(e['azp'], e['reason'], e['decision']) for e in events] == [
        ('lab-cli', 'insufficient_scope', 'would_deny'), ('lab-cli', None, 'allow')]
    assert events[0]['required_scope'] == 'rag-api:todos'


def test_enforced_denial_is_always_logged(client_for, caplog):
    c = client_for('enforce', 'ingest')
    with caplog.at_level('WARNING', logger='rag-auth'):
        for _ in range(3):
            c.post('/api/ingest', json={})
    denials = [m for m in caplog.messages if '"decision": "deny"' in m]
    assert len(denials) == 3


def test_index_routes_still_need_index_scope(client_for):
    c = client_for('enforce', 'todos')
    r = c.post('/retrieve', json={'query': 'q', 'k': 3}, headers=bearer(token(scope='rag-api:todos')))
    assert r.status_code == 403
