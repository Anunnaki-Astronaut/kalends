"""Retrospective contract: /status exposes recoverable configuration states.

The sidebar/calendar page cannot tell apart "no owner contract", "gateway
disconnected", "not configured", "missing Apple ID" and "missing app-specific
password" if the backend only reports one boolean. This pins the backend half of
that contract with synthetic settings and synthetic secrets only.

Run externally with tests/run-isolated-docker.sh --python tests/test_connection_states.py.
No production imports or network are performed by authoring this file.
"""
import json
import sys
import types

import pytest
from fastapi import FastAPI

from test_routes import api, load, run_async  # `api` fixture monkeypatches status_data

STATES = [
    ('', '', 'not_configured'),
    ('', 'synthetic-secret', 'missing_apple_id'),
    ('synthetic@example.invalid', '', 'missing_credential'),
    ('synthetic@example.invalid', 'synthetic-secret', 'connected'),
]


def fake_store():
    return types.SimpleNamespace(all=lambda: {}, migration_notice=lambda: None)


def configure(module, monkeypatch, apple_id, secret):
    """Point a freshly loaded plugin_api module at synthetic settings/secrets.

    test_routes' shared `api` fixture replaces status_data with a stub, so the
    real contract is exercised through an independent cold load.
    """
    secret_module = types.ModuleType('agent.secret_scope')
    secret_module.get_secret_str = lambda _key: secret
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secret_module)
    monkeypatch.setattr(module, 'settings', lambda: {'server_url': module.DEFAULT_URL, 'apple_id': apple_id})
    monkeypatch.setattr(module, 'permission_store', lambda cfg=None: fake_store())


@pytest.mark.parametrize('apple_id,secret,expected', STATES)
def test_status_reports_distinguishable_state(monkeypatch, apple_id, secret, expected):
    real = load()
    configure(real, monkeypatch, apple_id, secret)
    data = real.status_data()
    assert data['state'] == expected
    assert data['connected'] == (expected == 'connected')
    assert data['configured'] == data['connected']
    assert data['network_verified'] is False
    assert data['has_apple_id'] == bool(apple_id)
    assert data['has_credential'] == bool(secret)
    # Only booleans, never the secret value or the account address, are public.
    encoded = json.dumps(data)
    assert 'synthetic-secret' not in encoded
    assert 'synthetic@example.invalid' not in encoded
    assert not any('password' in key.lower() for key in data)


@pytest.mark.parametrize('apple_id,secret,expected', STATES)
def test_status_route_pairs_state_with_verified_owner(api, monkeypatch, apple_id, secret, expected):
    real = load()
    configure(real, monkeypatch, apple_id, secret)
    monkeypatch.setattr(api, 'owner_identity', lambda: 'opaque-A')
    monkeypatch.setattr(api, 'status_data', real.status_data)
    app = FastAPI()

    @app.middleware('http')
    async def admit(request, call_next):
        request.state.inprocess_admitted = True
        return await call_next(request)

    app.include_router(api.router)

    async def send():
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://localhost') as client:
            return await client.get('/status')

    reply = run_async(send())
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body['owner_id'] == 'opaque-A'
    assert body['state'] == expected
    assert body['connected'] == (expected == 'connected')


def test_status_never_fabricates_owner_and_keeps_existing_fields(monkeypatch):
    """The owner token is stamped by _execute, never by status_data. Configuration
    state must not be confused with the owner/activation contract."""
    real = load()
    configure(real, monkeypatch, 'synthetic@example.invalid', 'synthetic-secret')
    data = real.status_data()
    assert 'owner_id' not in data
    assert data['server_url'] == 'https://caldav.icloud.com'
    assert data['credential_location'] == 'Selected Hermes backend profile'
    assert data['permissions'] == {}
    assert data['connected'] is True
    assert data['configured'] is True
    assert data['network_verified'] is False


def test_credentials_presence_never_contacts_apple(monkeypatch):
    real = load()
    configure(real, monkeypatch, 'synthetic@example.invalid', 'synthetic-secret')
    monkeypatch.setattr(real, 'service', lambda: pytest.fail('status must not discover or authenticate'))
    assert real.status_data()['network_verified'] is False
