from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
import threading
from contextlib import nullcontext
from typing import Callable

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from pydantic import BaseModel


def _agent_runtime():
    # A private, file-rooted package works even when dashboard loads before agent.
    root = Path(__file__).resolve().parents[1] / 'icloud_caldav'
    name = '_icloud_dashboard_' + __import__('hashlib').sha256(str(root).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, root/'__init__.py', submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return __import__('importlib').import_module(name+'.runtime')


_runtime = _agent_runtime()
public_error = __import__('importlib').import_module(_runtime.__package__+'.errors').public_error
PermissionDenied = __import__('importlib').import_module(_runtime.__package__+'.exceptions').PermissionDenied
DEFAULT_URL, PLUGIN_ID, SECRET_ENV = _runtime.DEFAULT_URL, _runtime.PLUGIN_ID, _runtime.SECRET_ENV
permission_store, service, settings = _runtime.permission_store, _runtime.service, _runtime.settings
owner_identity = _runtime.owner_identity
validate_permissions = __import__("importlib").import_module(_runtime.__package__+".storage").validate_permissions
special = __import__("importlib").import_module(_runtime.__package__+".special_views")
_slots = threading.BoundedSemaphore(4)
_weather_slot = threading.BoundedSemaphore(1)
_special_slots = {key: threading.BoundedSemaphore(1) for key in (*special.VENDORS, "automation", "readiness")}
_owner_lock = threading.RLock()


def _authenticated(request: Request):
    from hermes_cli.plugin_host_http_auth import admitted_for_sensitive_route
    if not admitted_for_sensitive_route(request):
        raise HTTPException(401, 'authentication required')
    # This bit is injected by private IPC ASGI state, never by a header.
    if getattr(request.state, 'hosted_http_authenticated', False) is True:
        return
    if request.url.scheme != 'https' and request.url.hostname not in {'127.0.0.1','::1','localhost','testserver'}:
        raise HTTPException(400, 'plugin routes require HTTPS or loopback transport')


class OwnerRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handle(request):
            no_store = self.name.startswith('special_')
            try:
                response = await original(request)
            except HTTPException as exc:
                response = JSONResponse({'ok':False,'error':'admission','message':'Request admission failed.'},status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({'ok':False,'error':'invalid_request'},status_code=422)
            except Exception as exc:
                response = JSONResponse({'ok':False,'error':'invalid_request'} if no_store else public_error(exc),status_code=400)
            if no_store:
                response.headers['Cache-Control'] = 'no-store'
            return response
        return handle


router=APIRouter(dependencies=[Depends(_authenticated)],route_class=OwnerRoute)


class OwnerBody(BaseModel):
    owner_id: str


class PermissionsBody(OwnerBody):
    permissions: dict[str, object]


class QueryBody(OwnerBody):
    calendar_id: str
    start: str
    end: str


def status_data():
    from agent.secret_scope import get_secret_str
    cfg=settings()
    store=permission_store(cfg)
    # Public booleans only: the account address and the app-specific password are
    # never echoed. The states are distinguishable so the UI can offer the right
    # recovery step instead of one opaque "connected: false".
    has_apple_id=bool(cfg['apple_id'])
    has_credential=bool(get_secret_str(SECRET_ENV))
    if has_apple_id and has_credential:
        state='connected'
    elif not has_apple_id and not has_credential:
        state='not_configured'
    elif not has_apple_id:
        state='missing_apple_id'
    else:
        state='missing_credential'
    # Legacy connected/state fields mean credentials present, NOT an Apple probe.
    # Only a successful owner-scoped discovery in the UI verifies network access.
    weather_module = __import__('importlib').import_module(_runtime.__package__ + '.weather')
    import hmac, hashlib, json
    try:
        weather_scope = weather_module.snapshot()
        weather_revision = hmac.new(_runtime._OWNER_KEY, json.dumps(weather_scope).encode(), hashlib.sha256).hexdigest()
    except Exception:
        weather_scope = ("", False, None, None, None)
        weather_revision = hmac.new(_runtime._OWNER_KEY, b"weather-off", hashlib.sha256).hexdigest()
    return {'plugin_version': sys.modules[_runtime.__package__].__version__,
            'connected':state=='connected', 'state':state,
            'weather_enabled': weather_scope[1], 'weather_revision': weather_revision,
            'configured':state=='connected', 'network_verified':False,
            'has_apple_id':has_apple_id, 'has_credential':has_credential,
            'server_url':DEFAULT_URL,
            'credential_location':'Selected Hermes backend profile', 'permissions':store.all(), 'migration_notice':store.migration_notice()}


async def _execute(
    action, expected=None, *, serialized=True, slots=_slots,
    failure_stage: Callable[[], str | None] | None = None,
):
    # No unbounded executor queue. Contextvars (profile scope) follow run_sync.
    if not slots.acquire(blocking=False):
        return JSONResponse({'ok':False,'error':'busy'},status_code=429)
    def work():
        with _owner_lock if serialized else nullcontext():
            owner=owner_identity()
            if expected is not None and expected != owner:
                return JSONResponse({'ok':False,'owner_id':owner,'error':'owner_changed'},status_code=409)
            try:
                result=action()
                current=owner_identity()
                if current != owner:
                    return JSONResponse({'ok':False,'owner_id':current,'error':'owner_changed'},status_code=409)
                return {'owner_id':owner, **result}
            except Exception as exc:
                error = public_error(exc)
                if error.get('error') == 'invalid_request' and failure_stage is not None:
                    try:
                        if owner_identity() == owner:
                            stage = failure_stage()
                            if stage in ('request_validation', 'provider_read', 'event_processing'):
                                error['failure_stage'] = stage
                    except Exception:
                        # Diagnostics must not replace the original public error.
                        pass
                return JSONResponse({**error,'owner_id':owner},status_code=400)
    try:
        return await anyio.to_thread.run_sync(work)
    finally:
        slots.release()


@router.get('/status')
async def status():
    return await _execute(status_data)


@router.post('/discover')
async def discover(body: OwnerBody):
    def work():
        with service() as svc:
            calendars=svc.client.discover_calendars()
            for row in calendars:
                row['access']=svc.permissions.access_for(row['id'])
                row['capabilities']=svc.permissions.capabilities_for(row['id'])
            return {'ok':True,'calendars':calendars,'migration_notice':svc.permissions.migration_notice()}
    return await _execute(work,body.owner_id)


@router.post('/query')
async def query(body: QueryBody):
    current_stage = None
    failed_stage = None
    failed_permissions = None

    def observe_phase(stage: str | None):
        nonlocal current_stage
        current_stage = stage

    def work():
        nonlocal failed_stage, failed_permissions
        skipped = {}
        with service() as svc:
            store = svc.permissions
            try:
                events = svc.query_events(
                    body.calendar_id, body.start, body.end,
                    phase_observer=observe_phase, skipped=skipped,
                )
            except ValueError:
                failed_stage = current_stage
                failed_permissions = svc.permissions
                raise
        if skipped:
            # Recheck after service exit before releasing diagnostic content.
            if store.capabilities_for(body.calendar_id)['read'] is not True:
                raise PermissionDenied('calendar read denied')
            return {'ok': True, 'events': events, 'skipped': skipped}
        return {'ok': True, 'events': events}

    def failure_stage():
        # Re-read local permissions after service exit, immediately before
        # emitting diagnostics; never retain a capabilities snapshot.
        if failed_permissions is not None and failed_permissions.capabilities_for(body.calendar_id)['read'] is True:
            return failed_stage
        return None

    return await _execute(work, body.owner_id, failure_stage=failure_stage)


@router.post('/weather')
async def weather(body: OwnerBody):
    from importlib import import_module
    return await _execute(import_module(_runtime.__package__ + '.weather').forecast, body.owner_id, serialized=False, slots=_weather_slot)


async def _special_body(request, keys=(), cap=512):
    # Stream and cap even without a Content-Length; no validation error may echo input.
    data = bytearray()
    with anyio.fail_after(3):
        async for chunk in request.stream():
            if len(data) + len(chunk) > cap:
                raise HTTPException(413)
            data.extend(chunk)
    body = json.loads(data)
    if (not isinstance(body, dict) or set(body) != {"owner_id", *keys} or
            not isinstance(body["owner_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", body["owner_id"])):
        raise ValueError()
    return body


@router.post('/special/readiness')
async def special_readiness(request: Request):
    body = await _special_body(request)

    def work():
        states = {}
        for vendor in special.VENDORS:
            try:
                snapshot = special.feed_snapshot(vendor)
                state = special.feed_state(vendor, snapshot)
                states[vendor] = state if snapshot == special.feed_snapshot(vendor) else "unavailable"
            except Exception:
                states[vendor] = "unavailable"
        return {"ok": True, "feeds": states}
    return await _execute(work, body["owner_id"], serialized=False, slots=_special_slots["readiness"])


@router.post('/special/media/{vendor}')
async def special_media(request: Request, vendor: str):
    if vendor not in special.VENDORS:
        raise HTTPException(404)
    body = await _special_body(request, ("month", "zone"))
    bounds = special.month_bounds(body["month"], body["zone"])
    return await _execute(
        lambda: {"ok": True, **special.read_feed(vendor, lambda: special.feed_snapshot(vendor),
                                                 lambda: owner_identity() == body["owner_id"], bounds=bounds)},
        body["owner_id"], serialized=False, slots=_special_slots[vendor])


@router.post('/special/automations')
async def special_automations(request: Request):
    from hermes_constants import get_hermes_home
    from hermes_time import now
    body = await _special_body(request, ("month", "zone"))
    bounds = special.month_bounds(body["month"], body["zone"])

    def work():
        try:
            home = get_hermes_home()
            if owner_identity() == body["owner_id"]:
                result = special.automation_events(home, now(), bounds)
                if home == get_hermes_home():
                    return {"ok": True, **result}
        except Exception:
            pass
        return {"ok": True, "state": "unavailable", "events": []}
    return await _execute(work, body["owner_id"], serialized=False, slots=_special_slots["automation"])


@router.post('/special/automation-settings')
@router.put('/special/automation-settings')
async def special_automation_settings(request: Request):
    from hermes_constants import get_hermes_home
    from hermes_time import now
    saving = request.method == "PUT"
    body = await _special_body(request, ("ids",) if saving else (), 12_000 if saving else 512)
    ids = special.selection(body["ids"]) if saving else None

    def work():
        try:
            home = get_hermes_home()

            def current():
                return home == get_hermes_home() and owner_identity() == body["owner_id"]

            if current():
                jobs = special.scheduler_jobs(home, now())
                known = {row["id"] for row in jobs}
                selected = (special.save_jobs(home, [v for v in ids if v in known], current)
                            if saving else special.selected_jobs(home))
                if current():
                    return {"ok": True, "state": "loaded", "jobs": jobs,
                            "selected": [v for v in selected if v in known]}
        except Exception:
            pass
        return {"ok": True, "state": "unavailable", "jobs": [], "selected": []}
    return await _execute(work, body["owner_id"], serialized=False, slots=_special_slots["automation"])


@router.put('/permissions')
async def permissions(body: PermissionsBody):
    def work():
        validate_permissions(body.permissions)
        if len(body.permissions)>100:
            raise ValueError('invalid permissions')
        with service() as svc:
            discovered={row['id'] for row in svc.client.discover_calendars()}
            if set(body.permissions)-discovered:
                raise ValueError('undiscovered calendar')
            if owner_identity()!=body.owner_id:
                raise ValueError('owner changed during discovery')
            svc.permissions.replace(body.permissions)
            return {'ok':True,'permissions':svc.permissions.all(),'migration_notice':svc.permissions.migration_notice()}
    return await _execute(work,body.owner_id)


@router.post('/disconnect')
async def disconnect(body: OwnerBody):
    def work():
        from agent.secret_scope import get_secret_str
        # Hermes has no compare-and-swap credential writer. Clearing a shared
        # setting here could erase a replacement account selected concurrently.
        cfg=settings()
        store=permission_store(cfg)
        if owner_identity()!=body.owner_id:
            raise ValueError('owner changed before disconnect')
        store.replace({})
        remains=bool(get_secret_str(SECRET_ENV))
        return {'ok':True,'owner_after':owner_identity(),'credential_removed':not remains,'credential_present':remains,
                'message':'Calendar access revoked. No credential is currently available.' if not remains else 'Calendar access revoked. Clear the retained credential and Apple ID in Agent settings to finish disconnecting.'}
    return await _execute(work,body.owner_id)
