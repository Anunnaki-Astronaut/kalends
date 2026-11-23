"""Stage 3 RED contract; synthetic files and existing loopback CalDAV only.

Storage: {"version": 2, "permissions": {calendar: {read/create/edit/delete: bool}}}.
All four keys are mandatory, no extras/coercion; writes without read are invalid.
replace() accepts explicit capability maps only. capabilities_for() and all()
return effective capability objects. access_for() may remain a display summary,
but must never authorize. migration_notice() returns nonempty user-facing text
for legacy files, None otherwise; reading must never rewrite legacy bytes.
Dashboard status/save expose permissions and migration_notice; discovery and
calendar-list tools expose capabilities per row and migration_notice at top level.
Stale string saves are rejected. Moves are Stage 5: source read+delete and
 destination read+create, never inferred from a display access summary.

Run externally with tests/run-isolated.sh tests/test_stage3_permissions.py.
No production imports or test execution are performed by authoring this file.
"""
import importlib
import json
import sys
import types
from contextvars import ContextVar

import httpx
import pytest
from fastapi import FastAPI

from icloud_caldav.client import CalDAVClient
from icloud_caldav.exceptions import PermissionDenied
from icloud_caldav.ics import build_event
from icloud_caldav.security import SecurityError
from icloud_caldav.service import CalendarService
from icloud_caldav.storage import PermissionStore
from test_caldav_core import CalDAVFixtureHandler, caldav_server
from test_routes import api, run_async

CAL = 'https://caldav.icloud.com/synthetic/'


def caps(*enabled):
    return {key: key in enabled for key in ('read', 'create', 'edit', 'delete')}


def write_file(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)
    return path.read_bytes()


def v2(calendar, value):
    return {'version': 2, 'permissions': {calendar: value}}


@pytest.mark.parametrize('legacy,expected', [
    ('off', caps()), ('read', caps('read')),
    ('read-write', caps('read', 'create')),
])
def test_legacy_effective_capabilities_without_disk_migration(tmp_path, legacy, expected):
    store = PermissionStore(tmp_path / 'permissions.json')
    before = write_file(store.path, {CAL: legacy})
    assert store.capabilities_for(CAL) == expected
    assert store.all() == {CAL: expected}
    assert store.capabilities_for(CAL + 'missing/') == caps()
    assert isinstance(store.migration_notice(), str) and store.migration_notice().strip()
    assert store.path.read_bytes() == before
    store.replace({CAL: caps('read', 'edit')})
    assert json.loads(store.path.read_bytes()) == v2(CAL, caps('read', 'edit'))
    assert store.migration_notice() is None
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_missing_store_defaults_deny_without_creating_file(tmp_path):
    store = PermissionStore(tmp_path / 'missing.json')
    assert store.capabilities_for(CAL) == caps()
    assert store.all() == {}
    assert store.migration_notice() is None
    assert not store.path.exists()


BAD_CAPS = [None, True, [], 'read-write', {}, {'read': True},
            {**caps('read'), 'admin': True}]
BAD_CAPS += [{**caps('read'), key: value}
             for key in caps() for value in (0, 1, 'true', 'false', None, [], {})]
BAD_CAPS += [caps(key) for key in ('create', 'edit', 'delete')]
BAD_DOCS = [[], None, {'version': 2}, {'permissions': {}},
            {'version': 2, 'permissions': []},
            {'version': 2, 'permissions': {}, 'extra': True}]
BAD_DOCS += [{'version': version, 'permissions': {CAL: caps('read')}}
             for version in (True, False, 1, 3, '2', 2.0, None)]
BAD_DOCS += [v2(CAL, value) for value in BAD_CAPS]
BAD_DOCS += [v2('', caps('read'))]


@pytest.mark.parametrize('payload', BAD_DOCS)
def test_malformed_persisted_schema_fails_closed_without_rewrite(tmp_path, payload):
    store = PermissionStore(tmp_path / 'permissions.json')
    before = write_file(store.path, payload)
    for read in (lambda: store.capabilities_for(CAL), store.all):
        with pytest.raises(SecurityError):
            read()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('value', BAD_CAPS)
def test_invalid_explicit_save_preserves_previous_bytes(tmp_path, value):
    store = PermissionStore(tmp_path / 'permissions.json')
    before = write_file(store.path, v2(CAL, caps('read')))
    with pytest.raises((ValueError, SecurityError)):
        store.replace({CAL: value})
    assert store.path.read_bytes() == before


@pytest.fixture
def wire_env(caldav_server, tmp_path, monkeypatch):
    calendar = caldav_server + '/calendars/work/'
    store = PermissionStore(tmp_path / 'permissions.json')
    client = CalDAVClient(caldav_server, 'apple@example.com', 'app-password',
                          allow_loopback_http=True)
    seen = []
    # Capture handler entry, proving requests reached the actual fixture server.
    for method in ('GET', 'PUT', 'DELETE', 'REPORT'):
        original = getattr(CalDAVFixtureHandler, 'do_' + method)
        def handle(self, _method=method, _original=original):
            seen.append(_method)
            return _original(self)
        monkeypatch.setattr(CalDAVFixtureHandler, 'do_' + method, handle)
    raw = build_event(title='Synthetic', start='2026-11-03', end='2026-11-04', uid='stage3')
    CalDAVFixtureHandler.resources['/calendars/work/seed.ics'] = ('"seed"', raw)
    try:
        yield CalendarService(client, store), calendar, seen
    finally:
        client.close()


def mutate(svc, calendar, operation):
    common = dict(explicit_user_request=True, operation_id='stage3-' + operation)
    if operation == 'create':
        return svc.create_event(calendar, title='Created', start='2026-11-03',
                                end='2026-11-04', **common)
    common.update(etag='"seed"')
    if operation == 'edit':
        return svc.update_event(calendar, calendar + 'seed.ics', title='Edited', **common)
    return svc.delete_event(calendar, calendar + 'seed.ics', **common)


@pytest.mark.parametrize('grant', ['create', 'edit', 'delete'])
@pytest.mark.parametrize('operation', ['create', 'edit', 'delete'])
def test_each_mutation_requires_its_own_capability(wire_env, grant, operation):
    svc, calendar, seen = wire_env
    write_file(svc.permissions.path, v2(calendar, caps('read', grant)))
    if grant == operation:
        assert mutate(svc, calendar, operation)['verified'] is True
        assert ('DELETE' if operation == 'delete' else 'PUT') in seen
    else:
        before = dict(CalDAVFixtureHandler.resources)
        with pytest.raises(PermissionDenied):
            mutate(svc, calendar, operation)
        assert seen == []
        assert CalDAVFixtureHandler.resources == before


@pytest.mark.parametrize('operation', ['create', 'edit', 'delete'])
@pytest.mark.parametrize('legacy', ['off', 'read', 'read-write'])
def test_legacy_summary_never_grants_edit_or_delete(wire_env, operation, legacy):
    svc, calendar, seen = wire_env
    before = write_file(svc.permissions.path, {calendar: legacy})
    if legacy == 'read-write' and operation == 'create':
        assert mutate(svc, calendar, operation)['verified'] is True
        assert 'PUT' in seen
    else:
        with pytest.raises(PermissionDenied):
            mutate(svc, calendar, operation)
        assert seen == []
    assert svc.permissions.path.read_bytes() == before


@pytest.mark.parametrize('operation', ['edit', 'delete'])
@pytest.mark.parametrize('replacement', [caps(), caps('read', 'create')])
def test_revocation_during_get_prevents_write(wire_env, monkeypatch, operation, replacement):
    svc, calendar, seen = wire_env
    write_file(svc.permissions.path, v2(calendar, caps('read', operation)))
    before = dict(CalDAVFixtureHandler.resources)
    original = CalDAVFixtureHandler.do_GET
    def revoke_then_reply(handler):
        write_file(svc.permissions.path, v2(calendar, replacement))
        return original(handler)
    monkeypatch.setattr(CalDAVFixtureHandler, 'do_GET', revoke_then_reply)
    with pytest.raises(PermissionDenied):
        mutate(svc, calendar, operation)
    assert seen == ['GET']
    assert CalDAVFixtureHandler.resources == before


@pytest.mark.parametrize('operation', ['create', 'edit', 'delete'])
def test_final_permission_check_follows_ledger_io(wire_env, monkeypatch, operation):
    svc, calendar, seen = wire_env
    write_file(svc.permissions.path, v2(calendar, caps('read', operation)))
    reserve = svc.operations.reserve
    def revoke_after_reserve(*args):
        reserve(*args)
        write_file(svc.permissions.path, v2(calendar, caps()))
    monkeypatch.setattr(svc.operations, 'reserve', revoke_after_reserve)
    with pytest.raises(PermissionDenied):
        mutate(svc, calendar, operation)
    assert seen == ([] if operation == 'create' else ['GET'])


def test_display_access_summary_is_not_authorization(wire_env, monkeypatch):
    svc, calendar, seen = wire_env
    write_file(svc.permissions.path, v2(calendar, caps()))
    monkeypatch.setattr(svc.permissions, 'access_for', lambda _: 'read-write')
    with pytest.raises(PermissionDenied):
        mutate(svc, calendar, 'create')
    assert seen == []


@pytest.fixture
def dashboard_env(api, monkeypatch, tmp_path):
    # Restore real status_data: the shared admission fixture replaces it.
    from test_routes import load
    real = load()
    package = api._runtime.__package__
    Store = importlib.import_module(package + '.storage').PermissionStore
    Service = importlib.import_module(package + '.service').CalendarService
    store = Store(tmp_path / 'dashboard.json')
    state = {'owner': 'opaque-A', 'discovered': [], 'after': None}
    class Client:
        def discover_calendars(self):
            state['discovered'].append(CAL)
            if state['after']:
                state['after']()
            return [{'id': CAL, 'name': 'Synthetic', 'url': CAL}]
        def close(self):
            pass
    secret = types.ModuleType('agent.secret_scope')
    secret.get_secret_str = lambda _: 'synthetic-password'
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secret)
    monkeypatch.setattr(api, 'owner_identity', lambda: state['owner'])
    monkeypatch.setattr(api, 'service', lambda: Service(Client(), store))
    monkeypatch.setattr(real, 'permission_store', lambda cfg=None: store)
    monkeypatch.setattr(real, 'settings', lambda: {'apple_id': 'synthetic@example.invalid'})
    monkeypatch.setattr(api, 'status_data', real.status_data)
    app = FastAPI()
    @app.middleware('http')
    async def admit(request, call_next):
        request.state.inprocess_admitted = True
        return await call_next(request)
    app.include_router(api.router)
    def request(method, path, body=None):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost') as client:
                return await client.request(method, path, json=body)
        return run_async(send())
    return store, state, request


def test_dashboard_exposes_migration_and_effective_permissions_without_rewrite(dashboard_env):
    store, _, request = dashboard_env
    before = write_file(store.path, {CAL: 'read-write'})
    status = request('GET', '/status')
    assert status.status_code == 200, status.text
    assert status.json()['permissions'] == {CAL: caps('read', 'create')}
    assert status.json()['migration_notice']
    discovery = request('POST', '/discover', {'owner_id': 'opaque-A'})
    assert discovery.status_code == 200, discovery.text
    assert discovery.json()['calendars'][0]['capabilities'] == caps('read', 'create')
    assert discovery.json()['migration_notice']
    assert store.path.read_bytes() == before


def test_dashboard_explicit_save_is_v2_and_clears_notice(dashboard_env):
    store, _, request = dashboard_env
    write_file(store.path, {CAL: 'read-write'})
    permissions = {CAL: caps('read', 'delete')}
    reply = request('PUT', '/permissions', {'owner_id': 'opaque-A', 'permissions': permissions})
    assert reply.status_code == 200, reply.text
    assert reply.json()['permissions'] == permissions
    assert reply.json()['migration_notice'] is None
    assert json.loads(store.path.read_bytes()) == {'version': 2, 'permissions': permissions}


@pytest.mark.parametrize('value', BAD_CAPS)
def test_dashboard_rejects_stale_strings_and_malformed_caps_without_save(dashboard_env, value):
    store, state, request = dashboard_env
    before = write_file(store.path, {CAL: 'read'})
    reply = request('PUT', '/permissions', {'owner_id': 'opaque-A', 'permissions': {CAL: value}})
    assert reply.status_code in (400, 422), reply.text
    assert reply.json()['ok'] is False
    assert state['discovered'] == []
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('during', [False, True])
def test_dashboard_owner_change_cannot_save_capabilities(dashboard_env, during):
    store, state, request = dashboard_env
    before = write_file(store.path, {CAL: 'off'})
    if during:
        state['after'] = lambda: state.update(owner='opaque-B')
    else:
        state['owner'] = 'opaque-B'
    reply = request('PUT', '/permissions',
                    {'owner_id': 'opaque-A', 'permissions': {CAL: caps('read', 'edit')}})
    assert reply.status_code in (400, 409), reply.text
    assert reply.json()['ok'] is False
    assert len(state['discovered']) == int(during)
    assert store.path.read_bytes() == before


def test_dashboard_rejects_undiscovered_calendar(dashboard_env):
    store, _, request = dashboard_env
    before = write_file(store.path, {CAL: 'off'})
    reply = request('PUT', '/permissions', {'owner_id': 'opaque-A',
                    'permissions': {CAL + 'unknown/': caps('read', 'edit')}})
    assert reply.status_code == 400, reply.text
    assert store.path.read_bytes() == before


def test_tools_list_exposes_effective_caps_and_migration_notice(tmp_path, monkeypatch):
    from icloud_caldav import tools
    store = PermissionStore(tmp_path / 'permissions.json')
    before = write_file(store.path, {CAL: 'read-write'})
    client = types.SimpleNamespace(discover_calendars=lambda: [{'id': CAL}], close=lambda: None)
    monkeypatch.setattr(tools, '_service', lambda: CalendarService(client, store))
    reply = json.loads(tools.calendars_list({}))
    assert reply['ok'] is True
    assert reply['result'][0]['capabilities'] == caps('read', 'create')
    assert reply['migration_notice']
    assert store.path.read_bytes() == before


def test_runtime_tool_calls_use_current_profile_and_account(tmp_path, monkeypatch):
    from icloud_caldav import runtime, tools
    scope = ContextVar('stage3_scope', default=('A', 'one@example.invalid'))
    storage = types.ModuleType('plugins.plugin_storage')
    storage.plugin_data_dir = lambda _: tmp_path / scope.get()[0]
    secret = types.ModuleType('agent.secret_scope')
    secret.get_secret_str = lambda _: 'synthetic-password'
    constants = types.ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: tmp_path / scope.get()[0]
    for name, module in [('plugins', types.ModuleType('plugins')),
                         ('plugins.plugin_storage', storage),
                         ('agent', types.ModuleType('agent')), ('agent.secret_scope', secret),
                         ('hermes_constants', constants)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(runtime, 'settings', lambda: {
        'server_url': runtime.DEFAULT_URL, 'apple_id': scope.get()[1]})
    wire = []
    class Client:
        def __init__(self, *_):
            pass
        def get(self, href):
            from icloud_caldav.client import Resource
            wire.append(scope.get())
            return Resource(href, '"seed"', build_event(title='Synthetic',
                            start='2026-11-03', end='2026-11-04', uid='scope'))
        def close(self):
            pass
    monkeypatch.setattr(runtime, 'CalDAVClient', Client)
    paths, owners = [], []
    for selected, grant in [(('A', 'one@example.invalid'), caps('read')),
                             (('B', 'one@example.invalid'), caps()),
                             (('A', 'two@example.invalid'), caps())]:
        token = scope.set(selected)
        try:
            store = runtime.permission_store()
            paths.append(store.path)
            owners.append(runtime.owner_identity())
            write_file(store.path, v2(CAL, grant))
        finally:
            scope.reset(token)
    assert len(set(paths)) == len(set(owners)) == 3
    for selected, allowed in [(('A', 'one@example.invalid'), True),
                              (('B', 'one@example.invalid'), False),
                              (('A', 'two@example.invalid'), False),
                              (('A', 'one@example.invalid'), True)]:
        token = scope.set(selected)
        try:
            reply = json.loads(tools.event_get({'calendar_id': CAL, 'href': CAL + 'seed.ics'}))
            assert reply['ok'] is allowed
            if not allowed:
                assert reply['error'] == 'permission'
        finally:
            scope.reset(token)
    assert wire == [('A', 'one@example.invalid')] * 2
