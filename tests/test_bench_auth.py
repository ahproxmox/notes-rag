"""bench/bench_auth.py: rag-bench client token for the scheduled benches (identity-lab todo 526)."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_spec = importlib.util.spec_from_file_location('bench_auth', Path(__file__).parent.parent / 'bench' / 'bench_auth.py')


def load(monkeypatch, tmp_path, env_line=None):
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    env = tmp_path / '.env'
    env.write_text('OPENROUTER_API_KEY=x\n' + (env_line or ''))
    monkeypatch.setattr(mod, 'ENV_FILE', str(env))
    monkeypatch.delenv('RAG_BENCH_CLIENT_SECRET', raising=False)
    return mod


def test_no_secret_no_header(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path)
    with patch.object(mod.requests, 'post') as post:
        assert mod.auth_headers() == {}
    post.assert_not_called()


def test_secret_from_env_file_fetches_and_caches(monkeypatch, tmp_path):
    mod = load(monkeypatch, tmp_path, 'RAG_BENCH_CLIENT_SECRET=s3cret\n')
    resp = MagicMock()
    resp.json.return_value = {'access_token': 'tok', 'expires_in': 300}
    with patch.object(mod.requests, 'post', return_value=resp) as post:
        assert mod.auth_headers() == {'Authorization': 'Bearer tok'}
        assert mod.auth_headers() == {'Authorization': 'Bearer tok'}
    assert post.call_count == 1
    assert post.call_args.kwargs['data'] == {'grant_type': 'client_credentials', 'client_id': 'rag-bench',
                                             'client_secret': 's3cret'}
