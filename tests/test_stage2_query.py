"""RED contract: POST /query delegates one calendar range to CalendarService.

Only route wiring/host identity are stubbed; service, policy, XML and ICS are real.
All transport is in-memory and permissions live under pytest's synthetic tmp_path.
"""
import importlib
import json
from contextlib import contextmanager
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest
from fastapi import FastAPI

from test_routes import api, run_async  # existing cold-load/admission fixture

CAL = 'https://caldav.icloud.com/synthetic/'
BODY = dict(owner_id='opaque-A', calendar_id=CAL,
            start='2026-11-01T00:00:00-04:00', end='2026-12-06T00:00:00-05:00')
ICS = '''BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:synthetic\r
DTSTART:20261103T150000Z\r
DTEND:20261103T160000Z\r
SUMMARY:Synthetic owner A event\r
END:VEVENT\r
END:VCALENDAR\r
'''


@pytest.fixture
def query_env(api, monkeypatch, tmp_path):
    # Use the dashboard's own package identity (including its exception types).
    package = api._runtime.__package__
    Client = importlib.import_module(package + '.client').CalDAVClient
    Service = importlib.import_module(package + '.service').CalendarService
    Store = importlib.import_module(package + '.storage').PermissionStore
    store = Store(tmp_path / 'permissions.json')
    store.replace({CAL: {"read": True, "create": False, "edit": False, "delete": False}})
    state: dict[str, Any] = dict(owner='opaque-A', admitted=True, after=None, wire=[], service_calls=[], closed=[], ics=ICS)
    monkeypatch.setattr(api, 'owner_identity', lambda: state['owner'])
    original = Service.query_events

    def query(self, *args, **kwargs):
        state['service_calls'].append((args, kwargs))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Service, 'query_events', query)

    def transport(request):
        state['wire'].append(request)
        assert request.method == 'REPORT'
        assert str(request.url) == CAL
        if state['after']:
            state['after']()
        rows = [(CAL + 'event.ics', state['ics']), *state.get('extra', [])]
        xml = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + ''.join(
            '<d:response><d:href>' + escape(url) + '</d:href><d:propstat><d:prop>'
            '<d:getetag>"fixture"</d:getetag><c:calendar-data>' + escape(ics) +
            '</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status>'
            '</d:propstat></d:response>' for url, ics in rows) + '</d:multistatus>')

        return httpx.Response(207, text=xml)

    @contextmanager
    def service():
        client = Client('https://caldav.icloud.com', 'synthetic', 'synthetic')
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
        try:
            with Service(client, store) as svc:
                yield svc
        finally:
            state['closed'].append(client._client.is_closed)
            if state.get('after_close'):
                state['after_close']()
    monkeypatch.setattr(api, 'service', service)
    app = FastAPI()

    @app.middleware('http')
    async def admission(request, call_next):
        request.state.inprocess_admitted = state['admitted']
        return await call_next(request)
    app.include_router(api.router)

    def post(body=None):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                return await client.post('/query', json=BODY if body is None else body,
                                         headers={'hosted_http_authenticated': 'true'})
        return run_async(request())
    return state, store, post


POINT = (ICS.replace('UID:synthetic', 'UID:private-fixture-uid')
         .replace('SUMMARY:Synthetic owner A event', 'SUMMARY:Frame pickup')
         .replace('DTEND:20261103T160000Z', 'DTEND:20261103T150000Z')
         .replace('END:VEVENT', 'DESCRIPTION:fixture-private-note\r\nLOCATION:fixture-private-location\r\nEND:VEVENT'))


def test_partial_query_diagnostics_share_response_budget(api, query_env, monkeypatch):
    state, _, post = query_env
    ordinary = post()
    assert ordinary.status_code == 200
    events = ordinary.json()['events']
    service_module = importlib.import_module(api._runtime.__package__ + '.service')
    monkeypatch.setattr(service_module, '_MAX_QUERY_EVENTS_BYTES',
                        len(json.dumps(events, ensure_ascii=False).encode('utf-8')))
    assert post().status_code == 200
    state['extra'] = [(CAL + 'invalid-interval.ics', POINT)]
    reply = post()
    assert reply.status_code == 400
    assert reply.json()['error'] == 'work_budget'
    assert 'skipped' not in reply.json()


def test_partial_query_retains_valid_sibling_and_names_the_rejected_event(query_env, caplog):
    state, _, post = query_env
    state['extra'] = [(CAL + 'fixture-private-resource.ics', POINT)]
    reply = post()
    assert reply.status_code == 200
    payload = reply.json()
    assert [e['title'] for e in payload['events']] == ['Synthetic owner A event']
    assert payload['skipped'] == {'count': 1, 'examples': [{
        'code': 'equal_start_end', 'title': 'Frame pickup', 'start': '2026-11-03T15:00:00Z'}]}
    assert len(state['wire']) == 1
    for marker in ('fixture-private-note', 'fixture-private-location', 'private-fixture-uid', 'fixture-private-resource.ics'):
        assert marker not in reply.text
    assert 'Frame pickup' not in caplog.text
    state['extra'][0] = (state['extra'][0][0], POINT.replace('DTEND:20261103T150000Z', 'DTEND:20261103T170000Z'))
    recovered = post().json()
    assert {e['title'] for e in recovered['events']} == {'Frame pickup', 'Synthetic owner A event'}
    assert 'skipped' not in recovered and len(state['wire']) == 2


def test_partial_query_caps_examples_and_reports_all_skipped(query_env):
    state, _, post = query_env
    state['ics'] = POINT.replace('Frame pickup', 'X' * 250)
    state['extra'] = [(CAL + f'sample-{n}.ics', POINT.replace('private-fixture-uid', f'point-{n}')) for n in range(7)]
    payload = post().json()
    assert payload['events'] == []
    assert payload['skipped']['count'] == 8
    assert len(payload['skipped']['examples']) == 5
    assert len(payload['skipped']['examples'][0]['title']) == 120


def test_partial_query_preserves_the_existing_resource_limit(query_env):
    state, _, post = query_env
    state['extra'] = [(CAL + f'point-{n}.ics', POINT) for n in range(99)]
    allowed = post()
    assert allowed.status_code == 200
    assert len(allowed.json()['events']) == 1
    assert allowed.json()['skipped']['count'] == 99
    state['extra'].append((CAL + 'all-skipped.ics', POINT))
    state['ics'] = POINT
    denied = post()
    assert denied.status_code >= 400 and 'skipped' not in denied.json()
    assert 'Frame pickup' not in denied.text
    state['extra'].pop()
    assert post().json()['skipped']['count'] == 100


@pytest.mark.parametrize('change', ['revoked', 'revoked_on_close', 'owner_changed', 'foreign_resource', 'later_invalid_resource'])
def test_partial_query_never_leaks_across_a_failure_boundary(query_env, change):
    state, store, post = query_env
    state['ics'] = POINT
    if change == 'revoked':
        state['after'] = lambda: store.replace({})
    elif change == 'revoked_on_close':
        state['after_close'] = lambda: store.replace({})
    elif change == 'owner_changed':
        state['after'] = lambda: state.update(owner='opaque-B')
    elif change == 'foreign_resource':
        state['extra'] = [('https://caldav.icloud.com/other/foreign.ics', POINT)]
    else:
        state['extra'] = [(CAL + 'unsupported.ics', ICS.replace('END:VEVENT', 'RRULE:FREQ=SECONDLY\r\nEND:VEVENT').replace('Synthetic owner A event', 'other event'))]
    reply = post()
    assert reply.status_code >= 400
    assert 'skipped' not in reply.json() and 'events' not in reply.json()
    assert 'Frame pickup' not in reply.text and 'private-fixture-uid' not in reply.text


@pytest.mark.parametrize('ics', [
    POINT.replace('DTEND:20261103T150000Z', 'DTEND:20261103T140000Z'),
    POINT.replace('DTEND:20261103T150000Z\r\n', ''),
    POINT.replace('DTSTART:20261103T150000Z', 'DTSTART;VALUE=DATE:20261103')
         .replace('DTEND:20261103T150000Z', 'DTEND;VALUE=DATE:20261103'),
    POINT.replace('20261103T150000Z', '20261103T150000'),
    POINT.replace('END:VEVENT', 'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT'),
    POINT.replace('END:VEVENT', 'RECURRENCE-ID:20261103T150000Z\r\nEND:VEVENT'),
    POINT.replace('END:VEVENT', 'DURATION:PT0S\r\nEND:VEVENT'),
    POINT.replace('END:VEVENT', 'DTEND:20261103T150000Z\r\nEND:VEVENT'),
    POINT.replace('20261103T150000Z', '20261003T150000Z'),
    POINT.replace('END:VEVENT', 'STATUS:CANCELLED\r\nEND:VEVENT'),
    POINT.replace('END:VCALENDAR', 'BEGIN:VTODO\r\nUID:other\r\nEND:VTODO\r\nEND:VCALENDAR'),
], ids=['reversed', 'no-explicit-end', 'all-day', 'floating', 'series', 'override', 'duration-too', 'duplicate-end', 'outside-range', 'cancelled', 'mixed-components'])
def test_unclassified_event_failures_stay_strict_and_redacted(query_env, ics):
    state, _, post = query_env
    state['ics'] = ics
    reply = post()
    assert reply.status_code >= 400
    assert 'skipped' not in reply.json() and 'Frame pickup' not in reply.text


def test_service_default_still_rejects_same_bounds(api, query_env):
    state, _, _ = query_env
    state['ics'] = POINT
    with api.service() as svc, pytest.raises(ValueError, match='^invalid event interval$') as error:
        svc.query_events(CAL, BODY['start'], BODY['end'])
    assert type(error.value) is ValueError


def test_query_uses_existing_service_and_wire_range(query_env):
    state, _, post = query_env
    reply = post()
    assert reply.status_code == 200, reply.text
    assert reply.json()['owner_id'] == 'opaque-A'
    assert reply.json()['events'][0]['title'] == 'Synthetic owner A event'
    assert set(reply.json()) == {'ok', 'owner_id', 'events'}
    assert len(state['service_calls']) == len(state['wire']) == 1
    assert b'start="20261101T040000Z"' in state['wire'][0].content
    assert b'end="20261206T050000Z"' in state['wire'][0].content
    assert state['closed'] == [True]


@pytest.mark.parametrize('recurrence', ['RRULE:FREQ=DAILY;COUNT=2', 'RDATE:20261104T150000Z'])
def test_query_encoded_byte_budget(api, monkeypatch, query_env, recurrence):
    state, _, post = query_env
    package = api._runtime.__package__
    service_module = importlib.import_module(package + '.service')
    tools = importlib.import_module(package + '.tools')
    monkeypatch.setattr(tools, '_service', api.service)
    args = {key: BODY[key] for key in ('calendar_id', 'start', 'end')}
    state['ics'] = ICS.replace('END:VEVENT', f'DESCRIPTION:Café ☕\\nNotes\r\n{recurrence}\r\nEND:VEVENT')

    rows = post().json()['events']
    assert [row['start'] for row in rows] == ['2026-11-03T15:00:00Z', '2026-11-04T15:00:00Z']
    assert all(row['description'] == 'Café ☕\nNotes' and row['href'] == CAL + 'event.ics' for row in rows)
    encoded_size = len(json.dumps(rows, ensure_ascii=False).encode('utf-8'))

    monkeypatch.setattr(service_module, '_MAX_QUERY_EVENTS_BYTES', encoded_size, raising=False)
    assert post().json()['events'] == rows
    assert json.loads(tools.events_query(args)) == {'ok': True, 'result': rows}

    monkeypatch.setattr(service_module, '_MAX_QUERY_EVENTS_BYTES', encoded_size - 1)
    reply = post()
    assert reply.status_code == 400
    error = reply.json()
    assert error.pop('owner_id') == BODY['owner_id']
    assert error['ok'] is False and error['error'] == 'work_budget' and 'events' not in error
    assert json.loads(tools.events_query(args)) == error


def test_query_requires_host_admission_not_spoofed_header(query_env):
    state, _, post = query_env
    state['admitted'] = False
    reply = post()
    assert reply.status_code == 401
    assert not state['service_calls'] and not state['wire']
    assert 'owner_id' not in reply.json()


def test_query_off_has_zero_transport(query_env):
    state, store, post = query_env
    store.replace({})
    reply = post()
    assert reply.status_code in (400, 403)
    assert reply.json()['error'] == 'permission'
    assert not state['wire']
    assert 'failure_stage' not in reply.json()


@pytest.mark.parametrize('during', [False, True])
def test_query_discards_pre_and_post_owner_switch(query_env, during):
    state, _, post = query_env
    if during:
        state['after'] = lambda: state.update(owner='opaque-B')
    else:
        state['owner'] = 'opaque-B'
    reply = post()
    assert reply.status_code == 409
    assert reply.json()['error'] == 'owner_changed'
    assert 'failure_stage' not in reply.json()
    assert 'Synthetic owner A event' not in reply.text
    assert len(state['wire']) == int(during)


def test_query_rechecks_permission_between_requests(query_env):
    state, store, post = query_env
    assert post().status_code == 200
    store.replace({})
    reply = post()
    assert reply.status_code in (400, 403)
    assert reply.json()['error'] == 'permission'
    assert len(state['wire']) == 1


def test_query_does_not_publish_result_revoked_during_transport(query_env):
    state, store, post = query_env
    state['after'] = lambda: store.replace({})
    reply = post()
    assert reply.status_code in (400, 403, 409)
    assert reply.json()['ok'] is False
    assert 'Synthetic owner A event' not in reply.text
    assert len(state['wire']) == 1


@pytest.mark.parametrize('change', [dict(owner_id=None), dict(start='2026-11-01'),
                                  dict(end='2026-10-01T00:00:00Z'),
                                  dict(end='2028-11-01T00:00:00Z'),
                                  dict(calendar_id='https://attacker.invalid/cal/')])
def test_query_invalid_or_unapproved_scope_never_transports(query_env, change):
    state, _, post = query_env
    reply = post({**BODY, **change})
    assert reply.status_code in (400, 403, 422)
    assert not state['wire']


def test_invalid_query_reports_only_the_fixed_failure_stage(query_env, caplog):
    state, _, post = query_env
    marker = 'SYNTHETIC_DIAGNOSTIC_PRIVATE_VALUE'

    def assert_stage(reply, wanted):
        assert reply.status_code == 400
        payload = reply.json()
        assert payload['error'] == 'invalid_request'
        assert payload.get('failure_stage') == wanted
        assert set(payload) == {'ok', 'owner_id', 'error', 'message', 'failure_stage'}
        assert marker not in reply.text and CAL not in reply.text

    assert_stage(post({**BODY, 'start': marker}), 'request_validation')
    assert not state['wire']

    def fail_read():
        raise ValueError(marker)

    state['after'] = fail_read
    assert_stage(post(), 'provider_read')
    assert len(state['wire']) == 1
    state['after'] = None
    state['ics'] = ICS.replace('END:VEVENT', 'RRULE:FREQ=SECONDLY\r\nEND:VEVENT').replace('Synthetic owner A event', marker)
    assert_stage(post(), 'event_processing')
    assert len(state['wire']) == 2
    state['ics'] = ICS
    assert set(post().json()) == {'ok', 'owner_id', 'events'}
    assert len(state['wire']) == 3
    bad_shape = post({**BODY, 'start': None})
    assert bad_shape.status_code == 422 and 'failure_stage' not in bad_shape.json()
    work_budget = post({**BODY, 'end': '2028-11-01T00:00:00Z'})
    assert work_budget.json()['error'] == 'work_budget' and 'failure_stage' not in work_budget.json()
    def switch_then_fail_read():
        state['owner'] = 'opaque-B'
        raise ValueError(marker)

    state['after'] = switch_then_fail_read
    switched_failure = post()
    assert switched_failure.status_code == 400
    assert switched_failure.json()['error'] == 'invalid_request'
    assert 'failure_stage' not in switched_failure.json()
    assert marker not in caplog.text


@pytest.mark.parametrize('during', ['provider_read', 'event_processing'])
def test_failed_query_omits_stage_if_read_permission_was_revoked(query_env, during):
    state, store, post = query_env

    def revoke():
        store.replace({})
        if during == 'provider_read':
            raise ValueError('SYNTHETIC_PRIVATE_TRANSPORT_ERROR')

    state['after'] = revoke
    state['ics'] = ICS.replace('END:VEVENT', 'RRULE:FREQ=SECONDLY\r\nEND:VEVENT')
    reply = post()
    assert reply.status_code == 400 and reply.json()['error'] == 'invalid_request'
    assert 'failure_stage' not in reply.json()
    assert 'SYNTHETIC_PRIVATE_TRANSPORT_ERROR' not in reply.text
    assert len(state['wire']) == 1


def test_busy_query_retains_exact_retry_contract(api, query_env):
    state, _, post = query_env
    acquired = 0
    try:
        while api._slots.acquire(blocking=False):
            acquired += 1
        assert acquired > 0
        reply = post()
        assert reply.status_code == 429 and reply.json() == {'ok': False, 'error': 'busy'}
        assert not state['wire'] and not state['service_calls']
    finally:
        for _ in range(acquired):
            api._slots.release()


@pytest.mark.parametrize('method', ['query_events', 'get_event'])
def test_service_rechecks_permission_after_read(tmp_path, method):
    from icloud_caldav.client import Resource
    from icloud_caldav.exceptions import PermissionDenied
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore

    store = PermissionStore(tmp_path / 'permissions.json')
    store.replace({CAL: {"read": True, "create": False, "edit": False, "delete": False}})

    class Client:
        def query(self, *_):
            store.replace({})
            return []  # Empty reads must recheck too.

        def get(self, href):
            store.replace({})
            return Resource(href, '"fixture"', ICS.encode())

    svc = CalendarService(Client(), store)
    with pytest.raises(PermissionDenied):
        if method == 'query_events':
            svc.query_events(CAL, BODY['start'], BODY['end'])
        else:
            svc.get_event(CAL, CAL + 'event.ics')
