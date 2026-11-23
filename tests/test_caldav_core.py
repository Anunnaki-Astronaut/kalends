from __future__ import annotations

import io
import httpx
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from icloud_caldav.client import CalDAVClient, ConflictError
from icloud_caldav.ics import build_event, update_event, events_in_range
from icloud_caldav.security import SecurityError, validate_caldav_url
from icloud_caldav.service import CalendarService, PermissionDenied
from icloud_caldav.storage import PermissionStore

def test_create_title_limit_applies_to_stored_trimmed_title():
    from icloud_caldav.ics import event_to_dict
    dates = {"start": "2026-01-01", "end": "2026-01-02", "uid": "synthetic-title-limit"}
    assert event_to_dict(build_event(title="  " + "X" * 500 + "  ", **dates))["title"] == "X" * 500
    with pytest.raises(ValueError, match="at most 500"):
        build_event(title="  " + "X" * 501 + "  ", **dates)


class CalDAVFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    resources: dict[str, tuple[str, bytes]] = {}
    counter = 0
    race = False
    failure = None

    def log_message(self, *_args):
        return

    def _authorized(self):
        import base64
        return self.headers.get("Authorization")=="Basic "+base64.b64encode(b"apple@example.com:app-password").decode()

    def _body(self) -> bytes:
        size = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(size)

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PROPFIND(self):
        if not self._authorized():
            self._send(401)
            return
        self._body()
        body = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/calendars/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response><d:response><d:href>/calendars/work/</d:href><d:propstat><d:prop><d:displayname>Work</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''
        self._send(207, body, {"Content-Type": "application/xml"})

    def do_REPORT(self):
        if not self._authorized():
            self._send(401)
            return
        import xml.etree.ElementTree as ET
        window=ET.fromstring(self._body()).find('.//{urn:ietf:params:xml:ns:caldav}time-range')
        start=datetime.strptime(window.attrib['start'],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        end=datetime.strptime(window.attrib['end'],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        rows = []
        for path, (etag, ics) in sorted(type(self).resources.items()):
            if not path.startswith(self.path.rstrip("/")+"/") or not events_in_range(ics,start,end):
                continue
            rows.append(f"<d:response><d:href>{path}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag><c:calendar-data><![CDATA[{ics.decode()}]]></c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + ''.join(rows) + '</d:multistatus>').encode()
        self._send(207, body, {"Content-Type": "application/xml"})

    def do_GET(self):
        if type(self).failure == "failed-readback":
            self._send(503)
            return
        if not self._authorized():
            self._send(401)
            return
        item = type(self).resources.get(self.path)
        if not item:
            self._send(404)
            return
        etag, body = item
        self._send(200, body, {"ETag": etag, "Content-Type": "text/calendar"})

    def do_PUT(self):
        data=self._body()
        if not self._authorized():
            self._send(401)
            return
        current = type(self).resources.get(self.path)
        if type(self).race and current:
            type(self).race=False
            current=('"concurrent"',current[1])
            type(self).resources[self.path]=current
        if self.headers.get("If-None-Match") == "*" and current:
            self._send(412)
            return
        if self.headers.get("If-Match") and (not current or self.headers["If-Match"] != current[0]):
            self._send(412)
            return
        type(self).counter += 1
        etag = f'"v{type(self).counter}"'
        type(self).resources[self.path] = (etag, data)
        if type(self).failure == "lost-put":
            if hasattr(self,"connection"):
                self.close_connection=True
                self.connection.close()
                return
            raise httpx.ReadError("synthetic committed write lost response")
        self._send(201 if current is None else 204, headers={"ETag": etag})

    def do_DELETE(self):
        if not self._authorized():
            self._send(401)
            return
        current = type(self).resources.get(self.path)
        if not current:
            self._send(404)
            return
        if self.headers.get("If-Match") != current[0]:
            self._send(412)
            return
        del type(self).resources[self.path]
        self._send(204)


@pytest.fixture(params=["in-process", "socket"])
def caldav_server(request, monkeypatch):
    CalDAVFixtureHandler.resources = {}
    CalDAVFixtureHandler.counter = 0
    CalDAVFixtureHandler.race = False
    CalDAVFixtureHandler.failure = None
    if request.param == "in-process":
        def transport(req):
            handler=object.__new__(CalDAVFixtureHandler)
            handler.path=req.url.path
            handler.headers=req.headers
            handler.rfile=io.BytesIO(req.content)
            replies=[]
            handler._send=lambda status,body=b"",headers=None:replies.append(httpx.Response(status,content=body,headers=headers))
            getattr(handler,'do_'+req.method)()
            return replies[0]
        original=CalDAVClient.__init__
        clients=[]
        def initialize(client,*args,**kwargs):
            original(client,*args,**kwargs)
            client._client.close()
            client._client=httpx.Client(transport=httpx.MockTransport(transport),auth=httpx.BasicAuth('apple@example.com','app-password'))
            clients.append(client)
        monkeypatch.setattr(CalDAVClient,'__init__',initialize)
        yield 'http://127.0.0.1:8989'
        for client in clients:client.close()
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), CalDAVFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_icloud_url_policy_allows_only_tls_icloud_hosts():
    assert validate_caldav_url("https://caldav.icloud.com").host == "caldav.icloud.com"
    assert validate_caldav_url("https://p123-caldav.icloud.com/abc").host == "p123-caldav.icloud.com"
    for bad in [
        "http://caldav.icloud.com", "https://icloud.com", "https://evil.example",
        "https://user:pass@caldav.icloud.com", "https://caldav.icloud.com:444",
    ]:
        with pytest.raises(SecurityError):
            validate_caldav_url(bad)


def test_permission_store_defaults_off_and_refuses_symlink(tmp_path: Path):
    store = PermissionStore(tmp_path / "permissions.json")
    assert store.access_for("/calendars/work/") == "off"
    store.replace({"/calendars/work/": {"read": True, "create": False, "edit": False, "delete": False}})
    assert store.access_for("/calendars/work/") == "read"
    assert (tmp_path / "permissions.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "permissions.json").unlink()
    (tmp_path / "target").write_text("{}")
    (tmp_path / "permissions.json").symlink_to(tmp_path / "target")
    with pytest.raises(SecurityError):
        store.replace({})


def test_local_server_real_crud_readback_conflict_and_denial(caldav_server: str, tmp_path: Path):
    client = CalDAVClient(caldav_server, "apple@example.com", "app-password", allow_loopback_http=True)
    calendars = client.discover_calendars()
    assert calendars == [{"id": f"{caldav_server}/calendars/work/", "name": "Work", "url": f"{caldav_server}/calendars/work/"}]

    store = PermissionStore(tmp_path / "permissions.json")
    store.replace({f"{caldav_server}/calendars/work/": {"read": True, "create": True, "edit": True, "delete": True}})
    service = CalendarService(client, store)
    created = service.create_event(
        f"{caldav_server}/calendars/work/", title="Review", start="2026-11-03T14:00:00Z",
        end="2026-11-03T15:00:00Z", explicit_user_request=True, operation_id="test-1",
    )
    assert created["title"] == "Review"
    assert created["etag"]
    event = service.get_event(f"{caldav_server}/calendars/work/", created["href"])
    assert event["uid"] == created["uid"]

    stale_etag = created["etag"]
    updated = service.update_event(
        f"{caldav_server}/calendars/work/", created["href"], etag=stale_etag, title="Updated Review",
        explicit_user_request=True, operation_id="test-2",
    )
    assert updated["title"] == "Updated Review"
    with pytest.raises(ConflictError):
        service.update_event(
            f"{caldav_server}/calendars/work/", created["href"], etag=stale_etag, title="Overwrite",
            explicit_user_request=True, operation_id="test-3",
        )

    store.replace({f"{caldav_server}/calendars/work/": {"read": True, "create": False, "edit": False, "delete": False}})
    with pytest.raises(PermissionDenied):
        service.delete_event(
            f"{caldav_server}/calendars/work/", created["href"], etag=updated["etag"], explicit_user_request=True, operation_id="test-4",
        )
    store.replace({f"{caldav_server}/calendars/work/": {"read": True, "create": True, "edit": True, "delete": True}})
    service.delete_event(
        f"{caldav_server}/calendars/work/", created["href"], etag=updated["etag"], explicit_user_request=True, operation_id="test-5",
    )
    assert service.query_events(
        f"{caldav_server}/calendars/work/", "2026-11-03T00:00:00Z", "2026-11-04T00:00:00Z"
    ) == []


def test_update_preserves_unknown_fields_and_rejects_series_or_invitation_mutation():
    raw = build_event(
        title="Original", start="2026-11-03", end="2026-11-04", uid="u-1",
    ).replace(b"END:VEVENT", b"X-CUSTOM:keep-me\r\nEND:VEVENT")
    changed = update_event(raw, title="Changed")
    assert b"SUMMARY:Changed" in changed
    assert b"X-CUSTOM:keep-me" in changed

    recurring = raw.replace(b"END:VEVENT", b"RRULE:FREQ=DAILY\r\nEND:VEVENT")
    with pytest.raises(ValueError, match="recurring"):
        update_event(recurring, title="No")
    invitation = raw.replace(b"END:VEVENT", b"ATTENDEE:mailto:person@example.com\r\nEND:VEVENT")
    with pytest.raises(ValueError, match="invitation"):
        update_event(invitation, title="No")


def test_range_is_bounded_and_timezone_aware(caldav_server: str, tmp_path: Path):
    client = CalDAVClient(caldav_server, "apple@example.com", "app-password", allow_loopback_http=True)
    store = PermissionStore(tmp_path / "permissions.json")
    store.replace({f"{caldav_server}/calendars/work/": {"read": True, "create": False, "edit": False, "delete": False}})
    service = CalendarService(client, store)
    with pytest.raises(ValueError, match="timezone"):
        service.query_events(f"{caldav_server}/calendars/work/", "2026-11-03T00:00:00", "2026-11-04T00:00:00Z")
    with pytest.raises(ValueError, match="366"):
        service.query_events(f"{caldav_server}/calendars/work/", "2026-01-01T00:00:00Z", "2028-01-01T00:00:00Z")


def test_recurring_reads_expand_within_range_and_series_delete_is_refused(caldav_server: str, tmp_path: Path):
    recurring = build_event(
        title="Standup", start="2026-11-03T14:00:00Z", end="2026-11-03T14:15:00Z", uid="daily-1",
    ).replace(b"END:VEVENT", b"RRULE:FREQ=DAILY;COUNT=10\r\nEND:VEVENT")
    CalDAVFixtureHandler.resources["/calendars/work/daily-1.ics"] = ('"v1"', recurring)
    client = CalDAVClient(caldav_server, "apple@example.com", "app-password", allow_loopback_http=True)
    store = PermissionStore(tmp_path / "permissions.json")
    store.replace({f"{caldav_server}/calendars/work/": {"read": True, "create": True, "edit": True, "delete": True}})
    service = CalendarService(client, store)
    events = service.query_events(
        f"{caldav_server}/calendars/work/", "2026-11-03T00:00:00Z", "2026-11-06T00:00:00Z"
    )
    assert [event["start"] for event in events] == [
        "2026-11-03T14:00:00Z", "2026-11-04T14:00:00Z", "2026-11-05T14:00:00Z",
    ]
    with pytest.raises(ValueError, match="recurring"):
        service.delete_event(
            f"{caldav_server}/calendars/work/", f"{caldav_server}/calendars/work/daily-1.ics", etag='"v1"', explicit_user_request=True, operation_id="test-6",
        )


def test_conditional_put_race_after_preflight(caldav_server,tmp_path):
    client=CalDAVClient(caldav_server,'apple@example.com','app-password',allow_loopback_http=True)
    p=PermissionStore(tmp_path/'p.json');collection=caldav_server+'/calendars/work/';p.replace({collection:{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(client,p)
    try:
        created=svc.create_event(collection,title='Original',start='2026-11-03',end='2026-11-04',explicit_user_request=True,operation_id='race-create')
        CalDAVFixtureHandler.race=True
        with pytest.raises(ConflictError):
            svc.update_event(collection,created['href'],etag=created['etag'],title='No overwrite',explicit_user_request=True,operation_id='race-update')
        assert svc.get_event(collection,created['href'])['title']=='Original'
    finally:client.close()


@pytest.mark.parametrize('failure',['lost-put','failed-readback'])
def test_committed_write_is_reconciled_without_duplicate(caldav_server,tmp_path,failure):
    from icloud_caldav.client import IndeterminateError
    client=CalDAVClient(caldav_server,'apple@example.com','app-password',allow_loopback_http=True)
    p=PermissionStore(tmp_path/'p.json');collection=caldav_server+'/calendars/work/';p.replace({collection:{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(client,p)
    CalDAVFixtureHandler.failure=failure
    args=dict(title='Day',start='2026-11-03',end='2026-11-04',explicit_user_request=True,operation_id='ambiguous-create')
    try:
        with pytest.raises(IndeterminateError) as exc:svc.create_event(collection,**args)
        href=exc.value.href
        with pytest.raises(IndeterminateError):svc.create_event(collection,**args)
        assert len(CalDAVFixtureHandler.resources)==1
        CalDAVFixtureHandler.failure=None
        assert svc.get_event(collection,href)['title']=='Day'
    finally:client.close()


def test_strict_report_dst_offset_and_all_day_overlap(caldav_server,tmp_path,monkeypatch):
    import os,time
    old=os.environ.get('TZ');monkeypatch.setenv('TZ','America/New_York');time.tzset()
    client=CalDAVClient(caldav_server,'apple@example.com','app-password',allow_loopback_http=True)
    p=PermissionStore(tmp_path/'p.json');collection=caldav_server+'/calendars/work/';p.replace({collection:{"read": True, "create": True, "edit": True, "delete": True}})
    svc=CalendarService(client,p)
    try:
        svc.create_event(collection,title='DST',start='2026-11-01T01:30:00-04:00',end='2026-11-01T01:30:00-05:00',explicit_user_request=True,operation_id='dst')
        svc.create_event(collection,title='All day',start='2026-11-01',end='2026-11-02',explicit_user_request=True,operation_id='day')
        rows=svc.query_events(collection,'2026-11-01T05:45:00Z','2026-11-01T06:15:00Z')
        assert {r['title'] for r in rows}=={'DST','All day'}
        assert svc.query_events(collection,'2026-11-02T00:00:00Z','2026-11-03T00:00:00Z')==[]
    finally:
        client.close()
        if old is None:os.environ.pop('TZ',None)
        else:os.environ['TZ']=old
        time.tzset()
