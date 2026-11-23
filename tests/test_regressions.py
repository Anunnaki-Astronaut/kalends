"""Desired-behavior regressions adapted from the independent review probes."""
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from types import SimpleNamespace
import httpx
import pytest
from dateutil import rrule
from icloud_caldav.client import CalDAVClient
from icloud_caldav.ics import build_event, update_event, event_to_dict, events_in_range
from icloud_caldav.security import SecurityError

RAW = build_event(title='Synthetic', start='2026-11-03T14:00:00Z', end='2026-11-03T15:00:00Z', uid='synthetic')
START = datetime.fromisoformat('2026-11-03T00:00:00+00:00')
END = datetime.fromisoformat('2026-11-06T00:00:00+00:00')

def client(transport):
    c = CalDAVClient('https://caldav.icloud.com', 'synthetic', 'synthetic')
    c._client.close()
    c._client = httpx.Client(transport=httpx.MockTransport(transport))
    return c

@pytest.mark.parametrize('field', [b'ATTENDEE:mailto:x@example.invalid', b'ORGANIZER:mailto:x@example.invalid', b'RDATE:20261104T140000Z', b'EXDATE:20261104T140000Z'])
def test_shared_mutation_refusal(field):
    from icloud_caldav.ics import mutation_eligible
    raw = RAW.replace(b'END:VEVENT', field + b'\r\nEND:VEVENT')
    with pytest.raises(ValueError): mutation_eligible(raw)
    with pytest.raises(ValueError): update_event(raw, title='Changed')

def test_calendar_method_refused():
    with pytest.raises(ValueError):
        update_event(RAW.replace(b'BEGIN:VCALENDAR', b'BEGIN:VCALENDAR\r\nMETHOD:REQUEST'), title='Changed')

@pytest.mark.parametrize('path', ['%2e%2e/x.ics','%2E%2e/x.ics','%252e%252e/x.ics','a%2fb.ics','a%5cb.ics','../x.ics','a//x.ics','x.ics?x=1','x%3fquery','x%23fragment','..;parameter'])
def test_ambiguous_path_rejected_before_transport(path):
    calls=[]
    c=client(lambda r: calls.append(r) or httpx.Response(200, content=RAW, headers={'etag':'"1"'}))
    try:
        with pytest.raises(SecurityError): c.get('/allowed/'+path)
        assert not calls
    finally: c.close()

def test_utc_report_and_local_range(monkeypatch):
    old=os.environ.get('TZ')
    monkeypatch.setenv('TZ','America/New_York'); time.tzset()
    calls=[]
    c=client(lambda r: calls.append(r.content) or httpx.Response(207,content=b'<d:multistatus xmlns:d="DAV:"/>'))
    try:
        c.query('https://caldav.icloud.com/allowed/', datetime.fromisoformat('2026-11-03T09:00:00-05:00'), END)
        assert b'20261103T140000Z' in calls[0]
        assert events_in_range(RAW, END, datetime.fromisoformat('2026-11-07T00:00:00+00:00')) == []
    finally:
        c.close()
        if old is None: os.environ.pop('TZ',None)
        else: os.environ['TZ']=old
        time.tzset()

def test_shard_retained():
    seen=[]
    c=client(lambda r: seen.append(str(r.url)) or (httpx.Response(201) if r.method=='PUT' else httpx.Response(200,content=RAW,headers={'etag':'"1"'})))
    try:
        c.create('https://p123-caldav.icloud.com/allowed/','synthetic',RAW)
        assert seen == ['https://p123-caldav.icloud.com/allowed/synthetic.ics']*2
    finally: c.close()

def test_detached_exception_reads():
    extra=b'BEGIN:VEVENT\r\nUID:synthetic\r\nRECURRENCE-ID:20261104T140000Z\r\nDTSTART:20261104T160000Z\r\nDTEND:20261104T170000Z\r\nEND:VEVENT\r\n'
    raw=RAW.replace(b'END:VEVENT',b'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT').replace(b'END:VCALENDAR',extra+b'END:VCALENDAR')
    rows=events_in_range(raw,START,END)
    assert sorted(r['start'] for r in rows)==['2026-11-03T14:00:00Z','2026-11-04T16:00:00Z','2026-11-05T14:00:00Z']

def test_readback_must_match_intent():
    c=client(lambda r: httpx.Response(204) if r.method=='PUT' else httpx.Response(200,content=RAW,headers={'etag':'"1"'}))
    try:
        with pytest.raises(Exception, match='indeterminate'):
            c.update('https://caldav.icloud.com/allowed/x.ics','"1"',update_event(RAW,title='Changed'))
    finally: c.close()

@pytest.mark.parametrize('field', [b'ATTENDEE:mailto:x@example.invalid',b'ORGANIZER:mailto:x@example.invalid',b'RDATE:20261104T140000Z',b'EXDATE:20261104T140000Z',b'METHOD:REQUEST'])
def test_service_refusal_sends_no_writes(tmp_path,field):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    raw=RAW.replace(b'END:VEVENT',field+b'\r\nEND:VEVENT')
    calls=[]
    c=client(lambda r: calls.append(r.method) or httpx.Response(200,content=raw,headers={'etag':'"1"'}))
    store=PermissionStore(tmp_path/'permissions.json'); store.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(c,store)
    try:
        for method in ('update_event','delete_event'):
            with pytest.raises(ValueError):
                getattr(svc,method)('https://caldav.icloud.com/allowed/','https://caldav.icloud.com/allowed/x.ics',etag='"1"',explicit_user_request=True,operation_id=method)
        assert calls==['GET','GET']
    finally: c.close()

@pytest.mark.parametrize('lost', ['put','readback'])
def test_ambiguous_create_is_durable_and_never_retried(tmp_path,lost):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    from icloud_caldav.client import IndeterminateError
    stored={}; writes=[]
    def transport(r):
        if r.method=='PUT':
            writes.append(str(r.url)); stored[str(r.url)]=r.content
            if lost=='put': raise httpx.ReadError('secret synthetic password')
            return httpx.Response(201)
        raise httpx.ReadError('secret synthetic password')
    c=client(transport)
    p=PermissionStore(tmp_path/'permissions.json');p.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": True, "edit": True, "delete": True}})
    args=dict(title='Test',start='2026-11-03',end='2026-11-04',explicit_user_request=True,operation_id='stable-123')
    try:
        for _ in range(2):
            with pytest.raises(IndeterminateError) as e:
                CalendarService(c,p).create_event('https://caldav.icloud.com/allowed/',**args)
            assert e.value.operation_id=='stable-123'
            assert e.value.href.startswith('https://caldav.icloud.com/allowed/')
        assert len(writes)==1
    finally: c.close()

def test_public_error_redaction():
    from icloud_caldav.tools import _run
    import json
    result=json.loads(_run(lambda: (_ for _ in ()).throw(ValueError('password=synthetic-secret https://user:pass@example.invalid'))))
    assert 'synthetic-secret' not in str(result)
    assert 'user:pass' not in str(result)

@pytest.mark.parametrize('destination', ['https://evil.invalid/x','https://p123-caldav.icloud.com/denied/x.ics','/denied/x.ics','/allowed/%2e%2e/x'])
def test_resource_redirect_never_leaves_scope(destination):
    calls=[]
    c=client(lambda r:calls.append(str(r.url)) or httpx.Response(307,headers={'location':destination}))
    try:
        with pytest.raises(SecurityError): c.get('https://caldav.icloud.com/allowed/x.ics')
        assert len(calls)==1
    finally:c.close()

def test_multistep_discovery_keeps_effective_shard_origin():
    seen=[]
    def xml(prop,href='/'):
        return ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>'+href+'</d:href><d:propstat><d:prop>'+prop+'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>').encode()
    def transport(r):
        seen.append(str(r.url))
        if r.url.host=='caldav.icloud.com':return httpx.Response(307,headers={'location':'https://p123-caldav.icloud.com/root/'})
        if r.url.path=='/root/':return httpx.Response(207,content=xml('<d:current-user-principal><d:href>principal/</d:href></d:current-user-principal>'))
        if r.url.path=='/root/principal/':return httpx.Response(207,content=xml('<c:calendar-home-set><d:href>home/</d:href></c:calendar-home-set>'))
        assert r.url.path=='/root/principal/home/'
        return httpx.Response(207,content=xml('<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Work</d:displayname>','work/'))
    c=client(transport)
    try:
        rows=c.discover_calendars()
        assert rows[0]['id']=='https://p123-caldav.icloud.com/root/principal/home/work/'
        assert len(seen)==4
    finally:c.close()

def test_cancelled_all_day_exception_and_exdate():
    raw=build_event(title='Day',start='2026-11-03',end='2026-11-04',uid='day')
    raw=raw.replace(b'END:VEVENT',b'RRULE:FREQ=DAILY;COUNT=4\r\nEXDATE;VALUE=DATE:20261104\r\nEND:VEVENT')
    cancelled=b'BEGIN:VEVENT\r\nUID:day\r\nRECURRENCE-ID;VALUE=DATE:20261105\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\n'
    raw=raw.replace(b'END:VCALENDAR',cancelled+b'END:VCALENDAR')
    assert [x['start'] for x in events_in_range(raw,START,END)]==['2026-11-03']

def test_high_frequency_work_is_refused():
    raw=RAW.replace(b'END:VEVENT',b'RRULE:FREQ=SECONDLY\r\nEND:VEVENT')
    with pytest.raises(ValueError,match='budget|unsupported'): events_in_range(raw,START,END)

def test_get_accepts_master_and_exception():
    raw=RAW.replace(b'END:VEVENT',b'RRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT')
    extra=b'BEGIN:VEVENT\r\nUID:synthetic\r\nRECURRENCE-ID:20261104T140000Z\r\nDTSTART:20261104T160000Z\r\nDTEND:20261104T170000Z\r\nEND:VEVENT\r\n'
    result=event_to_dict(raw.replace(b'END:VCALENDAR',extra+b'END:VCALENDAR'))
    assert result['recurring'] and len(result['exceptions'])==1

def test_repeat_update_returns_reconcile_before_reading(tmp_path):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    from icloud_caldav.client import IndeterminateError
    calls=[]; stored=[RAW]
    def transport(r):
        calls.append(r.method)
        if r.method=='PUT':stored[0]=r.content;return httpx.Response(204)
        return httpx.Response(200,content=stored[0],headers={'etag':'"1"'})
    c=client(transport);p=PermissionStore(tmp_path/'p.json');p.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(c,p)
    args=dict(etag='"1"',title='Changed',explicit_user_request=True,operation_id='update-once')
    try:
        svc.update_event('https://caldav.icloud.com/allowed/','https://caldav.icloud.com/allowed/x.ics',**args)
        seen=list(calls)
        with pytest.raises(IndeterminateError):svc.update_event('https://caldav.icloud.com/allowed/','https://caldav.icloud.com/allowed/x.ics',**args)
        assert calls==seen
    finally:c.close()

def test_semantic_readback_accepts_offset_normalization():
    from icloud_caldav.ics import semantically_equal
    same=build_event(title='Synthetic',start='2026-11-03T09:00:00-05:00',end='2026-11-03T10:00:00-05:00',uid='synthetic')
    assert semantically_equal(RAW,same)

def test_tool_client_closes_on_parser_failure(monkeypatch):
    from icloud_caldav import tools
    closed=[]
    class Service:
        def __enter__(self):return self
        def __exit__(self,*_):closed.append(True)
        def get_event(self,**_):raise ValueError('sensitive')
    monkeypatch.setattr(tools,'_service',Service)
    assert 'sensitive' not in tools.event_get({'calendar_id':'x','href':'y'})
    assert closed==[True]

@pytest.mark.parametrize('href',['https://caldav.icloud.com/allowed-else/x.ics','https://p123-caldav.icloud.com/allowed/x.ics','/allowed/x.ics','https://caldav.icloud.com/allowed/sub/x.ics'])
@pytest.mark.parametrize('method',['get_event','update_event','delete_event'])
def test_service_membership_rejects_before_transport(tmp_path,href,method):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    calls=[];c=client(lambda r:calls.append(r) or httpx.Response(500))
    p=PermissionStore(tmp_path/'p.json');p.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": True, "edit": True, "delete": True}})
    kwargs={} if method=='get_event' else dict(etag='"1"',explicit_user_request=True,operation_id='scope-test')
    try:
        with pytest.raises(SecurityError):getattr(CalendarService(c,p),method)('https://caldav.icloud.com/allowed/',href,**kwargs)
        assert calls==[]
    finally:c.close()

def test_query_rejects_server_returned_sibling():
    xml=b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/denied/x.ics</d:href><d:propstat><d:prop><d:getetag>1</d:getetag><c:calendar-data>untrusted</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>'
    c=client(lambda r:httpx.Response(207,content=xml))
    try:
        with pytest.raises(SecurityError):c.query('https://caldav.icloud.com/allowed/',START,END)
    finally:c.close()

def test_rdate_only_is_recurring_and_expanded():
    raw=RAW.replace(b'END:VEVENT',b'RDATE:20261104T140000Z\r\nEND:VEVENT')
    assert event_to_dict(raw)['recurring']
    assert len(events_in_range(raw,START,END))==2


@pytest.mark.parametrize('recurrence', [
    b'RRULE:FREQ=DAILY;COUNT=2',
    b'RDATE:20261104T140000Z,20261106T000000Z',
])
def test_recurrence_projects_master_once(monkeypatch, recurrence):
    import copy
    from icloud_caldav import ics

    deepcopy = copy.deepcopy
    project = ics._event_component_to_dict
    copied = []
    projected = []

    def count_copies(value, *args, **kwargs):
        if isinstance(value, ics.Event):
            copied.append(value)
        return deepcopy(value, *args, **kwargs)

    def count_projection(event):
        projected.append(event.decoded('DTSTART'))
        return project(event)

    monkeypatch.setattr(copy, 'deepcopy', count_copies)
    monkeypatch.setattr(ics, '_event_component_to_dict', count_projection)
    raw = RAW.replace(b'END:VEVENT', recurrence + b'\r\nEND:VEVENT')
    rows = events_in_range(raw, START, END)
    assert [(row['start'], row['end']) for row in rows] == [
        ('2026-11-03T14:00:00Z', '2026-11-03T15:00:00Z'),
        ('2026-11-04T14:00:00Z', '2026-11-04T15:00:00Z'),
    ]
    assert all(row['recurring'] and row['recurrence_id'] is None for row in rows)
    assert rows[0] is not rows[1]
    assert not copied
    assert projected == [datetime.fromisoformat('2026-11-03T14:00:00+00:00')]


def test_cancelled_detachment_suppresses_master_occurrence():
    raw = RAW.replace(b'END:VEVENT', b'RRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT')
    exception = (b'BEGIN:VEVENT\r\nUID:synthetic\r\n'
                 b'RECURRENCE-ID:20261104T140000Z\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\n')
    rows = events_in_range(raw.replace(b'END:VCALENDAR', exception + b'END:VCALENDAR'), START, END)
    assert [row['start'] for row in rows] == ['2026-11-03T14:00:00Z']


def test_occurrence_budget_checked_before_projecting_override(monkeypatch):
    from icloud_caldav import ics
    from icloud_caldav.exceptions import WorkBudgetError

    project = ics._event_component_to_dict

    def project_only_master(event):
        assert event.get('RECURRENCE-ID') is None
        return project(event)

    monkeypatch.setattr(ics, 'MAX_INSTANCES', 1)
    monkeypatch.setattr(ics, '_event_component_to_dict', project_only_master)
    exception = (b'BEGIN:VEVENT\r\nUID:synthetic\r\n'
                 b'RECURRENCE-ID:20261104T140000Z\r\nDTSTART:20261104T160000Z\r\n'
                 b'DTEND:20261104T170000Z\r\nEND:VEVENT\r\n')
    with pytest.raises(WorkBudgetError, match='occurrence budget exceeded'):
        events_in_range(RAW.replace(b'END:VCALENDAR', exception + b'END:VCALENDAR'), START, END)


def test_client_total_request_budget():
    calls=[]
    c=client(lambda r:calls.append(r) or httpx.Response(200,content=RAW,headers={'etag':'"1"'}))
    try:
        with pytest.raises(Exception,match='budget'):
            for _ in range(17):c.get('https://caldav.icloud.com/allowed/x.ics')
        assert len(calls)==16
    finally:c.close()

def test_write_redirect_to_same_resource_never_retries():
    from icloud_caldav.client import IndeterminateError
    calls=[]
    href='https://caldav.icloud.com/allowed/x.ics'
    c=client(lambda r:calls.append(r.method) or httpx.Response(307,headers={'location':href}))
    try:
        with pytest.raises(IndeterminateError):c.update(href,'"1"',RAW)
        assert calls==['PUT']
    finally:c.close()

def test_reconciliation_read_distinguishes_absence_from_failure(monkeypatch,tmp_path):
    import json
    from icloud_caldav import tools
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    p=PermissionStore(tmp_path/'p.json');p.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": False, "edit": False, "delete": False}})
    monkeypatch.setattr(tools,'_service',lambda:CalendarService(client(lambda r:httpx.Response(404)),p))
    result=json.loads(tools.event_get({'calendar_id':'https://caldav.icloud.com/allowed/','href':'https://caldav.icloud.com/allowed/x.ics'}))
    assert result['error']=='not_found'


def test_all_day_rrule_with_date_until_expands():
    raw=build_event(title='Day',start='2026-11-03',end='2026-11-04',uid='until-day')
    raw=raw.replace(b'END:VEVENT',b'RRULE:FREQ=DAILY;UNTIL=20261105\r\nEND:VEVENT')
    assert [row['start'] for row in events_in_range(raw,START,END)] == [
        '2026-11-03','2026-11-04','2026-11-05'
    ]


def _run_recurrence_probe(
    rule,
    *,
    event_start="2026-11-03T14:00:00Z",
    event_end="2026-11-03T15:00:00Z",
    range_start="2026-11-03T00:00:00+00:00",
    range_end="2026-11-04T00:00:00+00:00",
):
    code = textwrap.dedent(f'''\
        from datetime import datetime
        from icloud_caldav.ics import build_event, events_in_range

        raw = build_event(
            title="Budget probe",
            start="{event_start}",
            end="{event_end}",
            uid="budget-probe",
        ).replace(b"END:VEVENT", b"RRULE:{rule}\\r\\nEND:VEVENT")
        try:
            rows = events_in_range(
                raw,
                datetime.fromisoformat("{range_start}"),
                datetime.fromisoformat("{range_end}"),
            )
        except ValueError as exc:
            print(str(exc))
        else:
            print(f"RESULT:{{len(rows)}}")
    ''')
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )


def test_zero_interval_filtered_rule_is_promptly_refused():
    result = _run_recurrence_probe("FREQ=DAILY;INTERVAL=0;BYDAY=MO;COUNT=1")
    assert result.returncode == 0, result.stderr
    assert "recurrence rule is unsupported" in result.stdout


@pytest.mark.parametrize("rule", [
    "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;COUNT=1",
    "FREQ=WEEKLY;BYMONTH=2;BYDAY=MO;COUNT=1",
    "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30;COUNT=1",
    "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30;COUNT=1",
])
def test_accepted_no_yield_rule_has_a_bounded_internal_horizon(rule):
    result = _run_recurrence_probe(
        rule,
        range_start="2026-11-04T00:00:00+00:00",
        range_end="2026-11-05T00:00:00+00:00",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RESULT:0"


@pytest.mark.parametrize(("rule", "maximum_periods"), [
    ("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;COUNT=1", 4),
    ("FREQ=WEEKLY;BYMONTH=2;BYDAY=MO;COUNT=1", 4),
    ("FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30;COUNT=1", 4),
    ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30;COUNT=1", 3),
])
def test_supported_frequency_period_work_stops_at_query_horizon(monkeypatch, rule, maximum_periods):
    observed = {"periods": 0, "last_year": None}
    original_rebuild = rrule._iterinfo.rebuild

    def counting_rebuild(self, year, month):
        observed["last_year"] = year
        return original_rebuild(self, year, month)

    monkeypatch.setattr(rrule._iterinfo, "rebuild", counting_rebuild)
    for name in ("ddayset", "wdayset", "mdayset", "ydayset"):
        original = getattr(rrule._iterinfo, name)

        def counting_dayset(self, *args, _original=original):
            observed["periods"] += 1
            return _original(self, *args)

        monkeypatch.setattr(rrule._iterinfo, name, counting_dayset)

    raw = RAW.replace(b"END:VEVENT", f"RRULE:{rule}\r\nEND:VEVENT".encode())
    rows = events_in_range(
        raw,
        datetime.fromisoformat("2026-11-04T00:00:00+00:00"),
        datetime.fromisoformat("2026-11-05T00:00:00+00:00"),
    )
    assert rows == []
    assert observed["periods"] <= maximum_periods
    assert observed["last_year"] <= 2027


def test_query_events_enforces_one_cumulative_recurrence_budget(tmp_path):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore

    collection = "https://caldav.icloud.com/allowed/"
    raw = RAW.replace(b"END:VEVENT", b"RRULE:FREQ=YEARLY;COUNT=2\r\nEND:VEVENT")
    resources = [
        SimpleNamespace(href=f"{collection}{index}.ics", etag='"1"', data=raw)
        for index in range(100)
    ]

    class QueryClient:
        def query(self, *_):
            return resources

    store = PermissionStore(tmp_path / "permissions.json")
    store.replace({collection: {"read": True, "create": False, "edit": False, "delete": False}})
    service = CalendarService(QueryClient(), store)
    with pytest.raises(ValueError, match="recurrence work budget exceeded"):
        service.query_events(
            collection,
            "2026-01-01T00:00:00Z",
            "2027-01-01T00:00:00Z",
        )


def test_excessive_historical_internal_work_is_promptly_refused():
    result = _run_recurrence_probe(
        "FREQ=DAILY;COUNT=1",
        event_start="1900-01-01T14:00:00Z",
        event_end="1900-01-01T15:00:00Z",
    )
    assert result.returncode == 0, result.stderr
    assert "recurrence work budget exceeded" in result.stdout


@pytest.mark.parametrize("rule", [
    "FREQ=DAILY;INTERVAL=-1;COUNT=1",
    "FREQ=DAILY;COUNT=0",
    "FREQ=MONTHLY;BYMONTH=13;COUNT=1",
    "FREQ=MONTHLY;BYSETPOS=0;COUNT=1",
    "FREQ=WEEKLY;BYMONTHDAY=1;COUNT=1",
    "FREQ=DAILY;BYYEARDAY=1;COUNT=1",
    "FREQ=MONTHLY;BYWEEKNO=1;COUNT=1",
    "FREQ=DAILY;BYSETPOS=1;COUNT=1",
    "FREQ=WEEKLY;BYDAY=1MO;COUNT=1",
    "FREQ=DAILY;COUNT=1;UNTIL=20261105T140000Z",
])
def test_malformed_or_nonprogress_recurrence_rules_are_promptly_refused(rule):
    result = _run_recurrence_probe(rule)
    assert result.returncode == 0, result.stderr
    assert "recurrence rule is unsupported" in result.stdout


@pytest.mark.parametrize(("frequency", "second"), [
    ("DAILY", "2026-11-04T14:00:00Z"),
    ("WEEKLY", "2026-11-10T14:00:00Z"),
    ("MONTHLY", "2026-12-03T14:00:00Z"),
    ("YEARLY", "2027-11-03T14:00:00Z"),
])
def test_supported_recurrence_frequencies_remain_valid(frequency, second):
    raw = RAW.replace(
        b"END:VEVENT",
        f"RRULE:FREQ={frequency};COUNT=2\r\nEND:VEVENT".encode(),
    )
    rows = events_in_range(
        raw,
        datetime.fromisoformat("2026-11-03T00:00:00+00:00"),
        datetime.fromisoformat("2027-11-04T00:00:00+00:00"),
    )
    assert [row["start"] for row in rows] == ["2026-11-03T14:00:00Z", second]


def test_daily_recurrence_preserves_wall_time_across_dst():
    raw = b"\r\n".join([
        b"BEGIN:VCALENDAR", b"VERSION:2.0", b"BEGIN:VEVENT", b"UID:dst-series",
        b"DTSTART;TZID=America/New_York:20261031T090000",
        b"DTEND;TZID=America/New_York:20261031T100000",
        b"RRULE:FREQ=DAILY;COUNT=3", b"SUMMARY:DST series",
        b"END:VEVENT", b"END:VCALENDAR", b"",
    ])
    rows = events_in_range(
        raw,
        datetime.fromisoformat("2026-10-31T00:00:00+00:00"),
        datetime.fromisoformat("2026-11-03T00:00:00+00:00"),
    )
    assert [row["start"] for row in rows] == [
        "2026-10-31T09:00:00-04:00",
        "2026-11-01T09:00:00-05:00",
        "2026-11-02T09:00:00-05:00",
    ]


def test_this_and_future_exception_is_rejected_instead_of_misexpanded():
    raw=RAW.replace(b'END:VEVENT',b'RRULE:FREQ=DAILY;COUNT=4\r\nEND:VEVENT')
    exception=(b'BEGIN:VEVENT\r\nUID:synthetic\r\n'
               b'RECURRENCE-ID;RANGE=THISANDFUTURE:20261104T140000Z\r\n'
               b'DTSTART:20261104T160000Z\r\nDTEND:20261104T170000Z\r\nEND:VEVENT\r\n')
    with pytest.raises(ValueError,match='THISANDFUTURE'):
        events_in_range(raw.replace(b'END:VCALENDAR',exception+b'END:VCALENDAR'),START,END)


def test_definitive_conflict_releases_operation_reservation(tmp_path):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    from icloud_caldav.client import ConflictError
    calls=[]
    def transport(request):
        calls.append(request.method)
        if request.method=='GET':
            return httpx.Response(200,content=RAW,headers={'etag':'"1"'})
        return httpx.Response(412)
    c=client(transport)
    store=PermissionStore(tmp_path/'permissions.json')
    store.replace({'https://caldav.icloud.com/allowed/':{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(c,store)
    args=dict(etag='"1"',title='Changed',explicit_user_request=True,operation_id='definite-conflict')
    try:
        for _ in range(2):
            with pytest.raises(ConflictError):
                svc.update_event('https://caldav.icloud.com/allowed/','https://caldav.icloud.com/allowed/x.ics',**args)
        assert calls==['GET','PUT','GET','PUT']
    finally:c.close()


@pytest.mark.parametrize('method_value',['REQUEST','REPLY','CANCEL'])
@pytest.mark.parametrize('operation',['update_event','delete_event'])
def test_calendar_method_mutation_refusal_sends_no_write(tmp_path,method_value,operation):
    from icloud_caldav.service import CalendarService
    from icloud_caldav.storage import PermissionStore
    raw=RAW.replace(b'BEGIN:VCALENDAR',f'BEGIN:VCALENDAR\r\nMETHOD:{method_value}'.encode())
    calls=[]
    c=client(lambda request:calls.append(request.method) or httpx.Response(200,content=raw,headers={'etag':'"1"'}))
    store=PermissionStore(tmp_path/'permissions.json')
    collection='https://caldav.icloud.com/allowed/'
    store.replace({collection:{"read": True, "create": True, "edit": True, "delete": True}})
    kwargs=dict(etag='"1"',explicit_user_request=True,operation_id=f'{operation}-{method_value.lower()}')
    if operation=='update_event': kwargs['title']='Changed'
    try:
        with pytest.raises(ValueError):
            getattr(CalendarService(c,store),operation)(collection,collection+'x.ics',**kwargs)
        assert calls==['GET']
    finally:c.close()
