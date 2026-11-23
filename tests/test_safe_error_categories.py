"""Stage 1 public-error contract; synthetic inputs only, run by Forge's runner.

Exact messages below specify the proposed public copy. Exercise existing entry
points so missing classification fails assertions, not imports of new classes.
"""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from icloud_caldav.client import CalDAVClient, IndeterminateError
from icloud_caldav.errors import public_error
from icloud_caldav.ics import RecurrenceWorkBudget, events_in_range
from icloud_caldav.service import CalendarService
from icloud_caldav import tools
from test_compressed_responses import wire_server
from test_routes import load as load_dashboard


COLLECTION = "http://127.0.0.1:8765/calendar/"
HREF = COLLECTION + "synthetic.ics"
PRIVATE = "synthetic-secret password=fixture-only https://user:pass@example.invalid"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 3, tzinfo=timezone.utc)
MESSAGES = {
    "auth": "Calendar authentication failed. Check your Apple ID and app-specific password in backend settings.",
    "permission": "Calendar access denied. Check this calendar's access policy and account permissions.",
    "connectivity": "Cannot reach the calendar server. Check your network connection and try again.",
    "response_format": "The calendar server returned an unreadable response. Try again; if this continues, contact support.",
    "work_budget": "Calendar request exceeded safe processing limits. Use a smaller date range or fewer calendars.",
}


@contextmanager
def mock_client(handler):
    client = CalDAVClient(COLLECTION, "synthetic", "fixture-only", allow_loopback_http=True)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        yield client
    finally:
        client.close()


def check_public(result, category):
    assert PRIVATE not in json.dumps(result)
    assert "fixture-only" not in json.dumps(result)
    assert "user:pass" not in json.dumps(result)
    assert result == {"ok": False, "error": category, "message": MESSAGES[category]}


def check_failure(action, category):
    # A successful/empty result must fail this test too.
    with pytest.raises(Exception) as caught:
        action()
    check_public(public_error(caught.value), category)
    return caught.value


@pytest.mark.parametrize("operation", ["discover", "query", "get"])
@pytest.mark.parametrize("status,category", [(401, "auth"), (403, "permission")])
def test_http_status_categories(operation, status, category):
    seen = []
    def reply(request):
        seen.append(request.method)
        return httpx.Response(status, content=PRIVATE.encode())
    with mock_client(reply) as client:
        actions = {
            "discover": client.discover_calendars,
            "query": lambda: client.query(COLLECTION, START, END),
            "get": lambda: client.get(HREF),
        }
        check_failure(actions[operation], category)
    assert seen == [{"discover": "PROPFIND", "query": "REPORT", "get": "GET"}[operation]]


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failure_is_connectivity_in_tool_output(error_type):
    def fail(request):
        raise error_type(PRIVATE, request=request)
    with mock_client(fail) as client:
        check_public(json.loads(tools._run(lambda: client.get(HREF))), "connectivity")


def test_invalid_xml_is_response_format():
    with mock_client(lambda _: httpx.Response(207, content=("<broken>" + PRIVATE).encode())) as client:
        check_failure(lambda: client.query(COLLECTION, START, END), "response_format")


def test_wire_compression_decode_is_response_format():
    # Real loopback HTTP bytes pass through httpx's decoder, not decoded content.
    with wire_server({"/": (207, {"Content-Encoding": "gzip"}, PRIVATE.encode())}) as (client, seen):
        error = check_failure(client.discover_calendars, "response_format")
        assert isinstance(error.__cause__, httpx.DecodingError)
        assert len(seen) == 1


@pytest.mark.parametrize("budget", ["requests", "deadline", "body"])
def test_client_limits_are_work_budget(budget):
    seen = []
    def reply(request):
        seen.append(request.method)
        return httpx.Response(200, headers={"ETag": '"synthetic"'}, content=b"x" * 2_000_001)
    with mock_client(reply) as client:
        if budget == "requests":
            client._remaining_requests = 0
        elif budget == "deadline":
            client._deadline = 0
        check_failure(lambda: client.get(HREF), "work_budget")
    assert seen == (["GET"] if budget == "body" else [])


def test_recurrence_exhaustion_is_work_budget():
    raw = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:synthetic\r\nDTSTART:20260101T000000Z\r\nDTEND:20260101T010000Z\r\n"
        "RRULE:FREQ=DAILY;COUNT=2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    check_failure(lambda: events_in_range(raw, START, END, work_budget=RecurrenceWorkBudget(limit=0)), "work_budget")


def test_local_policy_denial_is_permission_in_real_tool(monkeypatch, tmp_path):
    def no_io(_):
        pytest.fail("Off policy must reject before transport")
    with mock_client(no_io) as client:
        permissions = SimpleNamespace(path=tmp_path / "synthetic.json", capabilities_for=lambda _: {"read": False, "create": False, "edit": False, "delete": False})
        service = CalendarService(client, permissions)
        monkeypatch.setattr(tools, "_service", lambda: service)
        check_public(json.loads(tools.event_get({"calendar_id": COLLECTION, "href": HREF})), "permission")


def test_discovery_route_preserves_safe_auth_error(monkeypatch):
    api = load_dashboard()
    monkeypatch.setattr(api, "owner_identity", lambda: "synthetic-owner")
    from importlib import import_module
    route_client = import_module(api._runtime.__package__ + '.client').CalDAVClient
    client = route_client(COLLECTION, 'synthetic', 'fixture-only', allow_loopback_http=True)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, content=PRIVATE.encode())))
    try:
        @contextmanager
        def service():
            yield SimpleNamespace(client=client)
        monkeypatch.setattr(api, "service", service)
        response = asyncio.run(api.discover(api.OwnerBody(owner_id="synthetic-owner")))
    finally:
        client.close()
    assert response.status_code >= 400
    payload = json.loads(response.body)
    assert payload.pop("owner_id") == "synthetic-owner"
    check_public(payload, "auth")


@pytest.mark.parametrize("failure", ["timeout", "verification_auth", "verification_decode"])
def test_ambiguous_write_stays_indeterminate(failure):
    seen = []
    def reply(request):
        seen.append(request.method)
        if request.method == "PUT":
            if failure == "timeout":
                raise httpx.ReadTimeout(PRIVATE, request=request)
            return httpx.Response(204)
        if failure == "verification_auth":
            return httpx.Response(401, content=PRIVATE.encode())
        raise httpx.DecodingError(PRIVATE, request=request)
    with mock_client(reply) as client:
        with pytest.raises(IndeterminateError) as caught:
            client.update(HREF, '"synthetic"', b"synthetic calendar bytes")
    result = public_error(caught.value)
    assert result["error"] == "indeterminate" and result["ok"] is False
    assert result["retry_safe"] is False
    assert result["reconcile"] == "icloud_event_get" and result["href"] == HREF
    assert PRIVATE not in json.dumps(result)
    assert seen == (["PUT"] if failure == "timeout" else ["PUT", "GET"])


def test_untyped_exception_text_does_not_determine_category():
    # Do not implement classification by matching arbitrary exception messages.
    for word in MESSAGES:
        result = public_error(RuntimeError(f"{word}: {PRIVATE}"))
        assert result["error"] == "request_failed"
        assert PRIVATE not in json.dumps(result)
    assert public_error(ValueError("invalid date " + PRIVATE))["error"] == "invalid_request"
