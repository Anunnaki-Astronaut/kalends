"""Cold import and request-local host admission; no live Hermes services."""
import importlib.util
import sys
import types
from pathlib import Path
import pytest
from fastapi import FastAPI
import asyncio
import httpx

ROOT=Path(__file__).resolve().parents[1]

def load():
    spec=importlib.util.spec_from_file_location('isolated_dashboard', ROOT/'dashboard/plugin_api.py')
    api=importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
    return api

@pytest.fixture
def api(monkeypatch,tmp_path):
    api=load()
    auth=types.ModuleType('hermes_cli.plugin_host_http_auth')
    auth.admitted_for_sensitive_route=lambda r: getattr(r.state,'hosted_http_authenticated',False) is True or getattr(r.state,'inprocess_admitted',False) is True
    monkeypatch.setitem(sys.modules,'hermes_cli',types.ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules,'hermes_cli.plugin_host_http_auth',auth)
    monkeypatch.setattr(api,'owner_identity',lambda:'opaque-A')
    monkeypatch.setattr(api,'status_data',lambda:{'connected':True,'permissions':{}})
    return api

@pytest.mark.parametrize('host,hosted,admitted,expected', [('plugin-host',True,True,200),('plugin-host',False,False,401),('localhost',False,True,200),('remote.invalid',False,True,400)])
def test_route_admission(api,host,hosted,admitted,expected):
    app=FastAPI()
    @app.middleware('http')
    async def admission(request,call_next):
        request.state.hosted_http_authenticated=hosted and admitted
        request.state.inprocess_admitted=not hosted and admitted
        return await call_next(request)
    app.include_router(api.router)
    async def run():
        async def tick():
            while True: await asyncio.sleep(.01)
        ticker=asyncio.create_task(tick())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as c:
            return await c.get('/status',headers={'hosted_http_authenticated':'true'})
    result=asyncio.run(run())
    assert result.status_code==expected
    if expected==200: assert result.json()['owner_id']=='opaque-A'
    else: assert 'owner_id' not in result.json()

def test_owner_rejected_before_mutation(api,monkeypatch):
    from starlette.requests import Request
    monkeypatch.setattr(api,'service',lambda: pytest.fail('no discovery on mismatched owner'))
    import asyncio
    async def run():
        async def tick():
            while True: await asyncio.sleep(.01)
        ticker=asyncio.create_task(tick())
        return await api.permissions(api.PermissionsBody(owner_id='opaque-B',permissions={}))
    result=asyncio.run(run())
    assert result.status_code==409
    assert b'opaque-A' in result.body

def run_async(coro):
    async def run():
        # Sandbox disallows socket creation, including asyncio's wakeup socket;
        # keep polling its selector while worker completions are delivered.
        async def tick():
            while True: await asyncio.sleep(.005)
        ticker=asyncio.create_task(tick())
        try:return await coro
        finally:ticker.cancel()
    return asyncio.run(run())

@pytest.mark.parametrize('removed,present',[(False,True),(True,True),(True,False),(False,False)])
def test_disconnect_reports_actual_secret_state(api,monkeypatch,removed,present):
    writes=[]
    secret=types.ModuleType('agent.secret_scope');secret.get_secret_str=lambda key:'external-secret' if present else ''
    monkeypatch.setitem(sys.modules,'agent',types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules,'agent.secret_scope',secret)
    monkeypatch.setattr(api,'settings',lambda:{'apple_id':'synthetic@example.invalid','server_url':api.DEFAULT_URL})
    monkeypatch.setattr(api,'permission_store',lambda cfg=None:types.SimpleNamespace(replace=lambda x:writes.append(x)))
    result=run_async(api.disconnect(api.OwnerBody(owner_id='opaque-A')))
    assert result['credential_removed']==(not present)
    assert result['credential_present']==present and result['owner_id']=='opaque-A'
    assert writes==[{}]
    assert 'external-secret' not in str(result)


def test_discover_discards_result_if_owner_changes_during_io(api,monkeypatch):
    owners=iter(['opaque-A','opaque-B'])
    monkeypatch.setattr(api,'owner_identity',lambda:next(owners,'opaque-B'))
    class Service:
        permissions=types.SimpleNamespace(access_for=lambda _id:'off',
            capabilities_for=lambda _id:dict.fromkeys(('read','create','edit','delete'),False),
            migration_notice=lambda:None)
        client=types.SimpleNamespace(discover_calendars=lambda:[{'id':'https://caldav.icloud.com/b/','name':'B'}])
        def __enter__(self):return self
        def __exit__(self,*_):pass
    monkeypatch.setattr(api,'service',Service)
    result=run_async(api.discover(api.OwnerBody(owner_id='opaque-A')))
    assert result.status_code==409
    assert b'caldav.icloud.com/b/' not in result.body


def test_route_error_owner_lookup_does_not_block_event_loop(api,monkeypatch):
    import time
    monkeypatch.setattr(api,'owner_identity',lambda:(time.sleep(.2) or 'slow-owner'))
    app=FastAPI()
    @app.middleware('http')
    async def admit(request,call_next):
        request.state.inprocess_admitted=True
        return await call_next(request)
    app.include_router(api.router)
    ticks=[]
    async def run():
        async def ticker():
            for _ in range(10):
                await asyncio.sleep(.01);ticks.append(1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
            response, _ = await asyncio.gather(client.put('/permissions',json={}),ticker())
            return response
    result=run_async(run())
    assert result.status_code==422
    assert len(ticks)==10

def test_io_is_off_loop_and_client_closes_on_failure(api,monkeypatch):
    import threading
    main=threading.get_ident(); threads=[];closed=[]
    class Service:
        def __enter__(self):return self
        def __exit__(self,*_):closed.append(True)
        @property
        def client(self):return self
        def discover_calendars(self):
            threads.append(threading.get_ident())
            raise ValueError('sensitive synthetic credential')
    monkeypatch.setattr(api,'service',Service)
    result=run_async(api.discover(api.OwnerBody(owner_id='opaque-A')))
    assert result.status_code==400 and closed==[True]
    assert threads[0]!=main
    assert b'sensitive' not in result.body and b'opaque-A' in result.body

def test_invalid_permission_value_never_discovers(api,monkeypatch):
    monkeypatch.setattr(api,'service',lambda:pytest.fail('invalid input before I/O'))
    result=run_async(api.permissions(api.PermissionsBody(owner_id='opaque-A',permissions={'x':'admin'})))
    assert result.status_code==400

def test_http_exception_detail_is_not_public(api,monkeypatch):
    from fastapi import HTTPException
    auth=sys.modules['hermes_cli.plugin_host_http_auth']
    def rejected(_):raise HTTPException(403,'synthetic-secret-password')
    monkeypatch.setattr(auth,'admitted_for_sensitive_route',rejected)
    app=FastAPI();app.include_router(api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
            return await c.get('/status')
    result=run_async(run())
    assert result.status_code==403 and 'owner_id' not in result.json()
    assert 'synthetic-secret' not in result.text

def test_backend_owner_identity_is_opaque_and_context_scoped(monkeypatch,tmp_path):
    from contextvars import ContextVar
    from icloud_caldav import runtime
    profile=ContextVar('test_profile',default='launch')
    cfg={'server_url':runtime.DEFAULT_URL,'apple_id':'synthetic@example.invalid'}
    secret_value=['synthetic-secret']
    constants=types.ModuleType('hermes_constants');constants.get_hermes_home=lambda:tmp_path/profile.get()
    secret=types.ModuleType('agent.secret_scope');secret.get_secret_str=lambda key:secret_value[0]
    monkeypatch.setitem(sys.modules,'hermes_constants',constants)
    monkeypatch.setitem(sys.modules,'agent',types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules,'agent.secret_scope',secret)
    monkeypatch.setattr(runtime,'settings',lambda:dict(cfg))
    first=runtime.owner_identity();assert runtime.owner_identity()==first
    profile.set('secondary');second=runtime.owner_identity();assert second!=first
    profile.set('launch');assert runtime.owner_identity()==first
    secret_value[0]='rotated-secret';assert runtime.owner_identity()!=first
    cfg['apple_id']='other@example.invalid';assert runtime.owner_identity()!=second
    assert len(first)==64 and 'synthetic' not in first
    monkeypatch.setattr(runtime,'settings',lambda: (_ for _ in ()).throw(ValueError('bad config synthetic-secret')))
    assert len(runtime.owner_identity())==64


def test_weather_owner_gate_and_late_owner_change(api, monkeypatch):
    import importlib
    weather = importlib.import_module(api._runtime.__package__ + '.weather')
    calls = []
    monkeypatch.setattr(weather, 'forecast', lambda: calls.append(True) or {'ok': True, 'days': [{'date': '2026-11-03', 'condition': 'rain'}]})
    result = run_async(api.weather(api.OwnerBody(owner_id='opaque-B')))
    assert result.status_code == 409 and not calls
    owners = iter(['opaque-A', 'opaque-B'])
    monkeypatch.setattr(api, 'owner_identity', lambda: next(owners, 'opaque-B'))
    result = run_async(api.weather(api.OwnerBody(owner_id='opaque-A')))
    assert result.status_code == 409
    assert b'2026-11-03' not in result.body


def test_weather_uses_sensitive_route_admission(api, monkeypatch):
    import importlib
    weather = importlib.import_module(api._runtime.__package__ + '.weather')
    monkeypatch.setattr(weather, 'forecast', lambda: pytest.fail('unauthenticated weather request'))
    app = FastAPI()
    app.include_router(api.router)
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            return await client.post('/weather', json={'owner_id': 'opaque-A'})
    assert run_async(send()).status_code == 401


@pytest.mark.parametrize("fail", [False, True])
def test_slow_weather_leaves_all_calendar_slots_and_owner_lock_free(api, monkeypatch, fail, caplog):
    import importlib
    import threading
    caplog.set_level("DEBUG")
    weather = importlib.import_module(api._runtime.__package__ + '.weather')
    started, release = threading.Event(), threading.Event()
    def forecast():
        started.set()
        assert release.wait(3)
        if fail:
            raise ValueError("PRIVATE vendor URL key")
        return {'ok': True, 'days': []}
    monkeypatch.setattr(weather, 'forecast', forecast)
    async def run():
        task = asyncio.create_task(api.weather(api.OwnerBody(owner_id='opaque-A')))
        try:
            for _ in range(100):
                if started.is_set(): break
                await asyncio.sleep(.005)
            assert started.is_set()
            acquired = []
            try:
                for _ in range(4):
                    assert api._slots.acquire(blocking=False)
                    acquired.append(True)
                assert not api._slots.acquire(blocking=False)
            finally:
                for _ in acquired: api._slots.release()
            assert api._owner_lock.acquire(blocking=False)
            api._owner_lock.release()
            busy = await api.weather(api.OwnerBody(owner_id='opaque-A'))
            assert busy.status_code == 429
            status = await asyncio.wait_for(api.status(), .5)
            assert status['connected'] is True
        finally:
            release.set()
            await task
    run_async(run())
    assert "PRIVATE" not in caplog.text


def test_status_version_and_weather_snapshot_failure_do_not_deny_calendars(monkeypatch):
    import importlib
    api = load()
    secret = types.ModuleType('agent.secret_scope')
    secret.get_secret_str = lambda key: 'synthetic-secret'
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.secret_scope', secret)
    monkeypatch.setattr(api, 'settings', lambda: {'apple_id': 'synthetic@example.invalid'})
    monkeypatch.setattr(api, 'permission_store', lambda cfg: types.SimpleNamespace(all=lambda: {}, migration_notice=lambda: None))
    weather = importlib.import_module(api._runtime.__package__ + '.weather')
    def fail(): raise ValueError('PRIVATE location key')
    monkeypatch.setattr(weather, 'snapshot', fail)
    result = api.status_data()
    assert result['plugin_version'] == sys.modules[api._runtime.__package__].__version__ == '0.3.0'
    assert result['connected'] and result['weather_enabled'] is False
    assert not any(value in str(result) for value in ('PRIVATE', 'synthetic-secret', 'synthetic@example.invalid'))
