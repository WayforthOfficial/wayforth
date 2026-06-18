"""CLOUD-7 regression — agent-secret encryption must fail closed (no zero key).

A missing/invalid AGENT_SECRET_KEY must never silently use an all-zero key.
In production it raises at import; elsewhere the cipher isn't built and any
encrypt/decrypt raises via _require_cipher.

Run: uv run pytest tests/test_cloud7_agent_secret_failclosed.py -v
"""
import importlib

import pytest

VALID = "a" * 64  # 64 hex chars


def _reload(monkeypatch, *, key=None, env="development"):
    if key is None:
        monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENT_SECRET_KEY", key)
    monkeypatch.setenv("ENVIRONMENT", env)
    import core.agent_secrets as m
    return importlib.reload(m)


def test_valid_key_round_trips(monkeypatch):
    m = _reload(monkeypatch, key=VALID)
    blob = m.encrypt_env({"API_TOKEN": "xyz"})
    assert m.decrypt_env(blob) == {"API_TOKEN": "xyz"}


def test_missing_key_non_prod_imports_but_use_raises(monkeypatch):
    m = _reload(monkeypatch, key=None, env="development")
    assert m._GCM is None
    with pytest.raises(RuntimeError):
        m.encrypt_env({"x": "y"})
    with pytest.raises(RuntimeError):
        m.decrypt_env(b"0123456789abcdef")


def test_missing_key_in_production_raises_on_import(monkeypatch):
    with pytest.raises(RuntimeError):
        _reload(monkeypatch, key=None, env="production")


def test_malformed_key_in_production_raises_on_import(monkeypatch):
    with pytest.raises(RuntimeError):
        _reload(monkeypatch, key="nothex", env="production")


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    # Leave the module in a valid state for any later importers in the session.
    yield
    monkeypatch.setenv("AGENT_SECRET_KEY", VALID)
    monkeypatch.setenv("ENVIRONMENT", "development")
    import core.agent_secrets as m
    importlib.reload(m)
