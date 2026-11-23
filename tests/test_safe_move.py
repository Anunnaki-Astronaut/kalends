"""Stage 5 RED contract. Forge runs via tests/run-isolated.sh externally.

Service move_event(calendar_id, href, *, destination_calendar_id, etag,
explicit_user_request, operation_id) returns a move outcome dictionary:
  status=completed: destination verified, source absence verified; verified=True.
  status=destination_only: copy verified, source known retained; verified=False.
  status=source_retained: source retained, no verified attributable copy; False.
  status=indeterminate: either resource's relevant state cannot be established; False.
Every outcome includes operation_id, source_href, destination_href, retry_safe=False;
partial outcomes include nonempty manual_action. Preflight refusals may raise the
existing typed errors. Once a PUT is attempted, partial state must be returned,
not hidden behind a generic exception. Tool ok means completed, not merely returned.

The existing Operations database owns admission, request fingerprint and phases.
No new ledger API/schema names prescribed. Repeats may only reconcile with reads;
no second PUT or DELETE. Completed repeats require fresh verification of BOTH sides.
"""
import gzip
import json
import socket
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from icloud_caldav.client import CalDAVClient, ConflictError, IndeterminateError
from icloud_caldav.exceptions import CalDAVError, PermissionDenied
from icloud_caldav.operations import Operations
from icloud_caldav.security import SecurityError
from icloud_caldav.service import CalendarService
from icloud_caldav.storage import PermissionStore
from test_caldav_core import CalDAVFixtureHandler
from http.server import ThreadingHTTPServer

RAW = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic//EN\r\n'
       b'X-WR-CALNAME:Keep calendar metadata\r\nBEGIN:VEVENT\r\n'
       b'UID:original-uid\r\nDTSTART;VALUE=DATE:20261106\r\n'
       b'DTEND;VALUE=DATE:20261107\r\nSUMMARY:Synthetic\r\n'
       b'X-PRIVATE:folded value\r\n retained\r\n'
       b'BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\n'
       b'DESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
SRC = '/calendars/source/seed.ics'
DST = '/calendars/destination/'


def caps(*values):
    return {key: key in values for key in ('read', 'create', 'edit', 'delete')}


@pytest.fixture
def move_wire(tmp_path):
    """Extend the existing authenticated TCP handler; no mocked HTTP transport."""
    state = SimpleNamespace(seen=[], mode=None, after=None, encoding=None,
                            entered=threading.Event(), release=threading.Event())

    class Handler(CalDAVFixtureHandler):
        resources = {SRC: ('"source-v1"', RAW)}
        counter = 0
        failure = None

        def record(self, body=b''):
            state.seen.append((self.command, self.path, dict(self.headers), body))

        def drop(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def do_GET(self):
            self.record()
            if state.after:
                state.after('GET', self.path)
            dest = self.path.startswith(DST)
            if dest and state.mode == 'readback-failed':
                return self._send(503)
            if dest and state.mode == 'redirect':
                return self._send(307, headers={'Location': '/calendars/source/seed.ics'})
            item = self.resources.get(self.path)
            if item and (state.encoding or (dest and state.mode in ('mismatch', 'bomb', 'bad-gzip'))):
                etag, body = item
                if dest and state.mode == 'mismatch':
                    body = body.replace(b'UID:original-uid', b'UID:alien')
                if dest and state.mode == 'bomb':
                    body = b'x' * 2_000_001
                headers = {'ETag': etag, 'Content-Type': 'text/calendar'}
                if state.encoding or state.mode in ('bomb', 'bad-gzip'):
                    headers['Content-Encoding'] = 'gzip'
                    body = gzip.compress(body)
                    if dest and state.mode == 'bad-gzip':
                        body = b'not gzip'
                return self._send(200, body, headers)
            return super().do_GET()

        def do_PUT(self):
            body = self._body()
            self.record(body)
            if state.mode == 'blocked-put':
                state.entered.set()
                if not state.release.wait(5):
                    return self._send(503)
            if state.mode == 'collision':
                self.resources[self.path] = ('"alien"', b'unrelated destination')
            if self.path in self.resources:
                return self._send(412)
            if self.headers.get('If-None-Match') != '*':
                return self._send(428)
            self.resources[self.path] = ('"dest-v1"', body)
            if state.after:
                state.after('PUT', self.path)
            if state.mode == 'lost-put':
                return self.drop()
            return self._send(201, headers={'ETag': '"dest-v1"'})

        def do_DELETE(self):
            self.record()
            return super().do_DELETE()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    store = PermissionStore(tmp_path / 'permissions.json')
    source, destination = base + '/calendars/source/', base + DST
    grants = {source: caps('read', 'delete'), destination: caps('read', 'create')}
    store.replace(grants)
    clients = []

    def fresh():
        client = CalDAVClient(base, 'apple@example.com', 'app-password', allow_loopback_http=True, timeout=2)
        clients.append(client)
        return CalendarService(client, PermissionStore(store.path))

    env = SimpleNamespace(state=state, resources=Handler.resources, source=source,
                          destination=destination, href=base + SRC, store=store,
                          grants=grants, fresh=fresh, root=tmp_path)
    env.svc = fresh()
    try:
        yield env
    finally:
        state.release.set()
        for client in clients:
            client.close()
        server.shutdown()
        thread.join(2)
        server.server_close()


def move(env, svc=None, **changes):
    args = dict(calendar_id=env.source, href=env.href,
                destination_calendar_id=env.destination, etag='"source-v1"',
                explicit_user_request=True, operation_id='move-1')
    args.update(changes)
    return (svc or env.svc).move_event(**args)


def writes(env):
    return [row for row in env.state.seen if row[0] in ('PUT', 'DELETE')]


def outcome(result, env, statuses):
    assert result['status'] in statuses
    assert result['verified'] is (result['status'] == 'completed')
    assert result['operation_id'] == 'move-1'
    assert result['source_href'] == env.href
    assert result['destination_href'].startswith(env.destination)
    assert result['retry_safe'] is False
    if result['status'] != 'completed':
        assert isinstance(result['manual_action'], str) and result['manual_action'].strip()
    return result


def test_move_verifies_exact_copy_and_retains_source(move_wire):
    e = move_wire
    result = outcome(move(e), e, {'destination_only'})
    put, = writes(e)
    assert put[0] == 'PUT'
    assert put[2]['If-None-Match'] == '*' and 'If-Match' not in put[2]
    assert put[3] == RAW  # UID, folding, metadata and alarm survive byte for byte.
    assert put[1].startswith(DST) and put[1] != DST + 'original-uid.ics'
    assert '/' not in put[1][len(DST):] and put[1].endswith('.ics')
    assert result['destination_href'] == e.destination + put[1][len(DST):]
    sequence = [(r[0], r[1]) for r in e.state.seen]
    assert sequence.index(('GET', SRC)) < sequence.index(('PUT', put[1]))
    assert sequence.index(('PUT', put[1])) < sequence.index(('GET', put[1]))
    assert e.resources[SRC] == ('"source-v1"', RAW)
    assert e.resources[put[1]][1] == RAW
    assert isinstance(e.svc.operations, Operations)
    assert e.svc.operations.path == e.store.path.with_name('operations.sqlite3')
    assert e.svc.operations.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(e.svc.operations.path) as db:
        assert db.execute('SELECT id FROM operations WHERE id=?', ('move-1',)).fetchone()


@pytest.mark.parametrize('mode,statuses', [
    ('lost-put', {'indeterminate', 'destination_only'}),
    ('readback-failed', {'indeterminate'}), ('mismatch', {'indeterminate', 'source_retained'}),
    ('collision', {'source_retained'}), ('redirect', {'indeterminate'}),
    ('bomb', {'indeterminate'}), ('bad-gzip', {'indeterminate'}),
])
def test_partial_outcomes_and_restart_never_repeat_writes(move_wire, mode, statuses):
    e = move_wire
    e.state.mode = mode
    first = outcome(move(e), e, statuses)
    before = list(writes(e))
    assert [r[0] for r in before] == ['PUT']
    assert e.resources[SRC] == ('"source-v1"', RAW)
    if mode == 'collision':
        assert e.resources[urlsplit(first['destination_href']).path][1] == b'unrelated destination'
    e.state.mode = None
    # A fresh service/client/store must honor disk state, not process memory.
    again = outcome(move(e, e.fresh()), e, {'destination_only', 'source_retained', 'indeterminate'})
    assert writes(e) == before
    assert again['destination_href'] == first['destination_href']


@pytest.mark.parametrize('side,missing', [('source', 'read'), ('source', 'delete'),
                                        ('destination', 'read'), ('destination', 'create')])
def test_each_collection_requires_its_own_capabilities(move_wire, side, missing):
    e = move_wire
    key = getattr(e, side)
    e.grants[key] = caps() if missing == 'read' else caps('read')
    e.store.replace(e.grants)
    with pytest.raises(PermissionDenied):
        move(e)
    assert not e.state.seen


@pytest.mark.parametrize('boundary', ['source-get', 'put', 'destination-get'])
@pytest.mark.parametrize('side', ['source', 'destination'])
def test_revocation_at_wire_boundaries_prevents_next_mutation(move_wire, boundary, side):
    e = move_wire
    def revoke(method, path):
        hit = ((boundary == 'source-get' and method == 'GET' and path == SRC)
               or (boundary == 'put' and method == 'PUT')
               or (boundary == 'destination-get' and method == 'GET' and path.startswith(DST)))
        if hit:
            e.grants[getattr(e, side)] = caps()
            e.store.replace(e.grants)
    e.state.after = revoke
    if boundary == 'source-get':
        with pytest.raises(PermissionDenied):
            move(e)
        assert not writes(e)
    else:
        outcome(move(e), e, {'destination_only', 'indeterminate'})
        assert [r[0] for r in writes(e)] == ['PUT']
    assert SRC in e.resources


@pytest.mark.parametrize('etag', ['', '*', 'W/"source-v1"', 'source-v1', '"a", "source-v1"', '"source-v2"'])
def test_source_etag_must_be_strong_single_exact(move_wire, etag):
    with pytest.raises((ValueError, ConflictError)):
        move(move_wire, etag=etag)
    assert not writes(move_wire)


@pytest.mark.parametrize('asserted', [False, None, 1, 'true'])
def test_explicit_request_is_strict(move_wire, asserted):
    with pytest.raises(PermissionDenied):
        move(move_wire, explicit_user_request=asserted)
    assert not move_wire.state.seen


@pytest.mark.parametrize('field,value', [
    ('destination_calendar_id', 'same'), ('destination_calendar_id', 'no-slash'),
    ('destination_calendar_id', 'https://example.invalid/calendar/'),
    ('href', 'sibling'), ('href', 'nested'), ('href', 'collection'),
    ('href', 'traversal'), ('href', 'encoded'), ('href', 'query'),
])
def test_collection_and_member_identity_fail_closed(move_wire, field, value):
    e = move_wire
    values = {'same': e.source, 'no-slash': e.destination.rstrip('/'),
              'sibling': e.destination + 'seed.ics', 'nested': e.source + 'nested/seed.ics',
              'collection': e.source, 'traversal': e.source + '../seed.ics',
              'encoded': e.source + '%2e%2e/seed.ics', 'query': e.href + '?x=1'}
    with pytest.raises((ValueError, PermissionDenied)):
        move(e, **{field: values.get(value, value)})
    assert not writes(e)


@pytest.mark.parametrize('line', [b'RRULE:FREQ=DAILY', b'RDATE;VALUE=DATE:20261108',
    b'EXDATE;VALUE=DATE:20261106', b'RECURRENCE-ID;VALUE=DATE:20261106',
    b'ATTENDEE:mailto:synthetic@example.invalid', b'ORGANIZER:mailto:synthetic@example.invalid',
    b'METHOD:REQUEST', b'DURATION:P1D'])
def test_mutation_eligibility_exclusions_retained(move_wire, line):
    e = move_wire
    e.resources[SRC] = ('"source-v1"', RAW.replace(b'END:VEVENT', line + b'\r\nEND:VEVENT'))
    with pytest.raises(ValueError):
        move(e)
    assert [r[0] for r in e.state.seen] == ['GET']


@pytest.mark.parametrize('change', ['href', 'etag', 'destination_calendar_id', 'calendar_id'])
def test_persisted_request_fingerprint_refuses_rebinding(move_wire, change):
    e = move_wire
    e.state.mode = 'lost-put'
    outcome(move(e), e, {'indeterminate', 'destination_only'})
    before = list(writes(e))
    other = e.source.replace('/source/', '/other/')
    e.grants[other] = caps('read', 'create', 'delete')
    e.store.replace(e.grants)
    values = dict(href=e.source + 'other.ics', etag='"source-v2"',
                  destination_calendar_id=other, calendar_id=other)
    with pytest.raises((ValueError, ConflictError, IndeterminateError)):
        move(e, e.fresh(), **{change: values[change]})
    assert writes(e) == before


@pytest.mark.parametrize('phase', ['copy_verified', 'delete_intent', 'completed'])
@pytest.mark.parametrize('destination_state', ['intact', 'modified', 'missing'])
def test_repeat_reconciles_both_resources(move_wire, phase, destination_state):
    e = move_wire
    first = outcome(move(e), e, {'destination_only'})
    target = urlsplit(first['destination_href']).path
    if phase != 'copy_verified':
        # Historical ledger state, with the source already absent.
        with sqlite3.connect(e.svc.operations.path) as db:
            db.execute('UPDATE operations SET move_phase=? WHERE id=?', (phase, 'move-1'))
        del e.resources[SRC]
    if destination_state == 'modified':
        e.resources[target] = ('"new"', RAW.replace(b'UID:original-uid', b'UID:alien'))
    elif destination_state == 'missing':
        del e.resources[target]
    offset, before = len(e.state.seen), list(writes(e))
    if destination_state == 'intact':
        expected = 'destination_only' if phase == 'copy_verified' else 'completed'
    else:
        expected = 'source_retained' if phase == 'copy_verified' and destination_state == 'missing' else 'indeterminate'
    outcome(move(e, e.fresh()), e, {expected})
    assert writes(e) == before
    assert {SRC, target} <= {r[1] for r in e.state.seen[offset:] if r[0] == 'GET'}
    if phase == 'copy_verified':
        assert e.resources[SRC] == ('"source-v1"', RAW)


@pytest.mark.parametrize('prior', ['create', 'delete', 'legacy'])
def test_existing_operations_share_move_namespace(move_wire, prior):
    e = move_wire
    if prior == 'legacy':
        e.svc.operations.reserve('move-1', e.href)
    elif prior == 'create':
        e.svc.create_event(e.destination, title='Other', start='2026-11-06', end='2026-11-07',
                           explicit_user_request=True, operation_id='move-1')
    else:
        e.svc.delete_event(e.source, e.href, etag='"source-v1"', explicit_user_request=True, operation_id='move-1')
    before = list(writes(e))
    with pytest.raises((ValueError, ConflictError, IndeterminateError)):
        move(e, e.fresh())
    assert writes(e) == before


def test_concurrent_services_admit_one_put(move_wire):
    e = move_wire
    e.state.mode = 'blocked-put'
    def attempt(svc):
        try:
            return move(e, svc)
        except (ConflictError, IndeterminateError) as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, e.svc)
        try:
            assert e.state.entered.wait(3), 'first call never reached PUT'
            second = pool.submit(attempt, e.fresh())
            second.result(timeout=3)
        finally:
            e.state.release.set()
        outcome(first.result(timeout=5), e, {'destination_only'})
    assert [r[0] for r in writes(e)] == ['PUT']


@pytest.mark.parametrize('budget', [0, 1, 2])
def test_request_budget_cannot_produce_false_success(move_wire, budget):
    e = move_wire
    e.svc.client._remaining_requests = budget
    if budget == 0:
        with pytest.raises(CalDAVError):
            move(e)
    elif budget == 1:
        # A budget refusal before PUT may be raised or represented as partial;
        # the transport proves no destination mutation occurred in either case.
        try:
            result = move(e)
        except CalDAVError:
            pass
        else:
            outcome(result, e, {'source_retained', 'indeterminate'})
    else:
        outcome(move(e), e, {'indeterminate'})
    assert len(e.state.seen) <= budget
    assert not any(r[0] == 'DELETE' for r in writes(e))


def test_compressed_source_and_readback_preserve_original_bytes(move_wire):
    e = move_wire
    e.state.encoding = 'gzip'
    outcome(move(e), e, {'destination_only'})
    assert writes(e)[0][3] == RAW


@pytest.mark.parametrize('unsafe', ['mode', 'symlink', 'full'])
def test_existing_ledger_security_and_bound_are_retained(move_wire, unsafe):
    e = move_wire
    ledger = e.svc.operations.path
    if unsafe == 'symlink':
        target = e.root / 'untouched'
        target.write_bytes(b'untouched')
        ledger.symlink_to(target)
    else:
        e.svc.operations.reserve('old', e.href)
        if unsafe == 'mode':
            ledger.chmod(0o644)
        else:
            with sqlite3.connect(ledger) as db:
                db.executemany('INSERT INTO operations (id, href) VALUES (?, ?)',
                               [(f'old-{i}', e.href) for i in range(9999)])
    with pytest.raises((ValueError, OSError)):
        move(e)
    assert not writes(e)
    if unsafe == 'symlink':
        assert target.read_bytes() == b'untouched'


def test_tool_schema_dispatch_and_partial_envelope(move_wire, monkeypatch):
    from icloud_caldav import runtime, schemas, tools
    name = 'icloud_event_move'
    assert name in schemas.MUTATIONS and name in tools.HANDLERS
    params = schemas.SCHEMAS[name]['parameters']
    required = {'calendar_id', 'href', 'destination_calendar_id', 'etag', 'explicit_user_request', 'operation_id'}
    assert set(params['required']) == required
    assert set(params['properties']) == required and params['additionalProperties'] is False
    e = move_wire
    monkeypatch.setattr(runtime, 'prepare_service', lambda: e.fresh)
    monkeypatch.setitem(sys.modules, 'tools.approval_prompt', SimpleNamespace(
        request_elicitation_consent=lambda *args, **kwargs: 'accept'))
    reply = json.loads(tools.HANDLERS[name](dict(calendar_id=e.source, href=e.href,
        destination_calendar_id=e.destination, etag='"source-v1"', explicit_user_request=True, operation_id='move-1')))
    assert reply['ok'] is False
    outcome(reply['result'], e, {'destination_only'})
    assert [r[0] for r in writes(e)] == ['PUT']


def test_move_reservation_blocks_other_mutation_kinds(move_wire):
    e = move_wire
    e.state.mode = 'lost-put'
    outcome(move(e), e, {'indeterminate', 'destination_only'})
    before = list(writes(e))
    svc = e.fresh()
    with pytest.raises((ValueError, ConflictError, IndeterminateError)):
        svc.create_event(e.destination, title='Different intention', start='2026-11-06',
                         end='2026-11-07', explicit_user_request=True, operation_id='move-1')
    with pytest.raises((ValueError, ConflictError, IndeterminateError)):
        svc.delete_event(e.source, e.href, etag='"source-v1"',
                         explicit_user_request=True, operation_id='move-1')
    assert writes(e) == before


def test_destination_name_is_deterministic_from_operation_not_original_uid(move_wire):
    e = move_wire
    first = outcome(move(e), e, {'destination_only'})
    first_path = urlsplit(first['destination_href']).path
    # Independent synthetic account storage simulates the same initial input.
    alternate = PermissionStore(e.root / 'isolated-account' / 'permissions.json')
    alternate.replace(e.grants)
    svc = e.fresh()
    svc = CalendarService(svc.client, alternate)
    e.resources.clear()
    e.resources[SRC] = ('"source-v1"', RAW)
    second = move(e, svc)
    assert second['destination_href'] == first['destination_href']
    e.resources[SRC] = ('"source-v1"', RAW)
    third = move(e, operation_id='move-2')
    assert third['destination_href'] != first['destination_href']
    assert e.resources[first_path][1] == RAW


def test_phases_are_durable_in_existing_database_before_wire_writes(move_wire):
    e = move_wire
    snapshots = []
    def snapshot(method, path):
        if method == 'PUT' or (method == 'GET' and path.startswith(DST)):
            with sqlite3.connect(f'file:{e.svc.operations.path}?mode=ro', uri=True) as db:
                row = db.execute('SELECT move_phase FROM operations WHERE id=?', ('move-1',)).fetchone()
                snapshots.append((method, row[0]))
    e.state.after = snapshot
    outcome(move(e), e, {'destination_only'})
    assert snapshots == [('PUT', 'put_intent'), ('GET', 'put_intent'), ('GET', 'copy_verified')]
    # Also enforce ownership: no parallel move database or sidecar JSON engine.
    assert {p.name for p in e.root.iterdir()} <= {
        'permissions.json', 'operations.sqlite3', 'operations.sqlite3-wal',
        'operations.sqlite3-shm', 'operations.sqlite3-journal'}


def test_named_timezone_components_are_copied_intact(move_wire):
    from test_named_timezone import resource
    e = move_wire
    raw = resource()
    e.resources[SRC] = ('"source-v1"', raw)
    outcome(move(e), e, {'destination_only'})
    assert writes(e)[0][3] == raw


@pytest.mark.parametrize('raw', [
    RAW.replace(b'END:VCALENDAR', RAW[RAW.index(b'BEGIN:VEVENT'):RAW.index(b'END:VCALENDAR')] + b'END:VCALENDAR'),
    RAW.replace(b'VERSION:2.0', b'VERSION:2.0\r\nMETHOD:REQUEST'),
    RAW.replace(b'DTSTART;VALUE=DATE:20261106', b'DTSTART:20261106T090000')
       .replace(b'DTEND;VALUE=DATE:20261107', b'DTEND:20261106T100000'),
])
def test_other_existing_mutation_exclusions(move_wire, raw):
    e = move_wire
    e.resources[SRC] = ('"source-v1"', raw)
    with pytest.raises(ValueError):
        move(e)
    assert not writes(e)


@pytest.mark.parametrize('identity', ['', '../escape', 'a' * 101, 'space here'])
def test_operation_identity_validation_before_transport(move_wire, identity):
    with pytest.raises(ValueError):
        move(move_wire, operation_id=identity)
    assert not move_wire.state.seen


def test_repeat_after_permission_revocation_does_not_reconcile(move_wire):
    e = move_wire
    outcome(move(e), e, {'destination_only'})
    before = list(e.state.seen)
    e.store.replace({})
    with pytest.raises(PermissionDenied):
        move(e, e.fresh())
    assert e.state.seen == before
