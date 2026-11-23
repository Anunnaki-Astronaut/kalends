import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest

from icloud_caldav import special_views as special


def test_scheduler_snapshot_projects_only_next_run_and_does_not_repair(tmp_path):
    cron = tmp_path / "cron"
    cron.mkdir()
    jobs = cron / "jobs.json"
    original = json.dumps({"jobs": [
        {"id": "one", "name": "Backup", "enabled": True, "next_run_at": "2027-04-09T12:30:00+00:00",
         "prompt": "SYNTHETIC_PRIVATE_PROMPT", "last_run_at": "2027-04-01"},
        {"id": "two", "name": "Paused", "enabled": False, "next_run_at": "2027-04-10T00:00:00Z"},
        {"id": "three", "name": "Past", "enabled": True, "next_run_at": "2027-04-01T00:00:00Z"},
        {"id": "manual", "name": "Manual", "enabled": True, "next_run_at": "2027-04-08T00:00:00Z",
         "manual_run_at": "2027-04-08T00:00:00Z"},
        {"id": "stale-pause", "name": "Stale pause", "enabled": True, "state": "scheduled",
         "paused_at": "2027-04-01T00:00:00Z", "next_run_at": "2027-04-08T00:00:00Z"},
        {"id": "invalid", "name": "Invalid time", "enabled": True, "next_run_at": "2027-04-08T00:00:00",
         "schedule": {"kind": "cron", "expr": "0 0 * * *"}},
        {"id": "missing", "name": "Missing time", "enabled": True,
         "schedule": {"kind": "cron", "expr": "0 0 * * *"}},
    ]})
    jobs.write_text(original)
    rows = special.scheduler_jobs(tmp_path, datetime(2027, 4, 2, tzinfo=timezone.utc))
    assert rows == [
        {"id": "one", "title": "Backup", "enabled": True, "start": "2027-04-09T12:30:00+00:00"},
        {"id": "two", "title": "Paused", "enabled": False, "start": None},
        {"id": "three", "title": "Past", "enabled": True, "start": None},
        {"id": "manual", "title": "Manual", "enabled": True, "start": None},
        {"id": "stale-pause", "title": "Stale pause", "enabled": False, "start": None},
        {"id": "invalid", "title": "Invalid time", "enabled": True, "start": None},
        {"id": "missing", "title": "Missing time", "enabled": True, "start": None},
    ]
    assert jobs.read_text() == original
    assert list(cron.iterdir()) == [jobs]
    now = datetime(2027, 4, 2, tzinfo=timezone.utc)
    bounds = special.month_bounds("2027-04", "UTC")
    assert special.automation_events(tmp_path, now, bounds) == {"state": "off", "events": []}
    special.save_jobs(tmp_path, ["one", "two", "three", "manual", "stale-pause", "invalid", "missing", "deleted"], lambda: True)
    assert special.automation_events(tmp_path, now, bounds) == {"state": "loaded", "events": [
        {"id": "one", "title": "Backup", "start": "2027-04-09T12:30:00+00:00"},
    ]}


@pytest.mark.parametrize("vendor,path,options", [
    ("sonarr", "/feed/v5/calendar/Sonarr.ics", "unmonitored=true&asAllDay=false&premieresOnly=true&tags=1,25"),
    ("sonarr", "/proxy/feed/v3/calendar/Sonarr.ics", "unmonitored=false&asAllDay=true&premieresOnly=false"),
    ("radarr", "/feed/v3/calendar/Radarr.ics", "unmonitored=false&asAllDay=true&tags=2,4"
     "&releaseTypes=cinemaRelease&releaseTypes=digitalRelease&releaseTypes=physicalRelease"),
])
@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
def test_generated_links(vendor, path, options, scheme, port):
    for query in ("apikey=SYNTHETIC_KEY", options + "&apikey=SYNTHETIC_KEY"):
        resource = f"{path}?{query}"
        url = f"{scheme}://192.168.0.2{resource}"
        assert special.feed_target(vendor, url, scheme == "http") == (
            scheme, "192.168.0.2", port, resource, "SYNTHETIC_KEY")
        if scheme == "http":
            with pytest.raises(ValueError):
                special.feed_target(vendor, url, False)


@pytest.mark.parametrize("vendor,path_query", [
    ("radarr", "/feed/v5/calendar/Radarr.ics"),
    ("sonarr", "/feed/v4/calendar/Sonarr.ics"),
    ("sonarr", "/Feed/v5/calendar/Sonarr.ics"),
    ("sonarr", "/feed/v5/calendar/Sonarr.ics/extra"),
    ("radarr", "/feed/v3/calendar/radarr.ics"),
    ("sonarr", "/feed/v3/calendar/sonarr.ics?releaseTypes=cinemaRelease"),
    ("radarr", "/feed/v3/calendar/Radarr.ics?premieresOnly=true"),
    *[("radarr", "/feed/v3/calendar/Radarr.ics?" + query) for query in (
        "unmonitored=True", "asAllDay=1", "unmonitored=true&unmonitored=false",
        "asAllDay=true&asAllDay=true", "apikey=A&apikey=B", "apikey=",
        "tags=1&tags=2", "tags=1,,2", "tags=1,a", "tags=" + "1" * 11, "tags=" + "1," * 32 + "2",
        "tags=1%2C2", "releaseTypes=unknown", "releaseTypes=cinemaRelease,physicalRelease",
        "releaseTypes=cinemaRelease&releaseTypes=cinemaRelease", "pastDays=30", "futureDays=30",
    )],
])
def test_reject_unapproved_vendor_paths_and_options(vendor, path_query):
    with pytest.raises(ValueError):
        special.feed_target(vendor, "https://10.0.0.1" + path_query, True)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/feed/v3/calendar/sonarr.ics",
    "https://169.254.169.254/feed/v3/calendar/sonarr.ics",
    "https://8.8.8.8/feed/v3/calendar/sonarr.ics",
    "https://synthetic.invalid/feed/v3/calendar/sonarr.ics",
    "https://10.0.0.1/%2e%2e/feed/v3/calendar/sonarr.ics",
    "https://10.0.0.1/feed/v3/calendar/radarr.ics",
    "https://10.0.0.1/feed/v3/calendar/sonarr.ics?unknown=yes",
    "https://10.0.0.1:65536/feed/v3/calendar/sonarr.ics",
    "http://10.0.0.1/feed/v3/calendar/sonarr.ics",
    "https://user@10.0.0.1/feed/v3/calendar/sonarr.ics",
    "https://10.0.0.1/feed/v3/calendar/sonarr.ics#",
    "webcal://10.0.0.1/feed/v3/calendar/sonarr.ics?apikey=SYNTHETIC_KEY",
])
def test_reject_unapproved_targets(url):
    with pytest.raises(ValueError):
        special.feed_target("sonarr", url, False)


def test_media_off_stale_and_error_do_not_publish_content():
    raw = "https://192.168.0.2:8989/proxy/feed/v3/calendar/sonarr.ics?apikey=SYNTHETIC_SECRET"
    def never(*_):
        pytest.fail("disabled feed must not call transport")
    assert special.read_feed("sonarr", lambda: ("a", False, False, raw), lambda: True, never) == {
        "state": "off", "events": []}

    def failed(*_):
        raise TimeoutError(raw)
    assert special.read_feed("sonarr", lambda: ("a", True, False, raw), lambda: True, failed) == {
        "state": "unavailable", "events": []}

    snapshots = iter([("a", True, False, raw), ("a", True, False, raw), ("b", True, False, raw)])
    assert special.read_feed("sonarr", lambda: next(snapshots), lambda: True,
                             lambda *_: b"BEGIN:VCALENDAR\nEND:VCALENDAR\n") == {
        "state": "unavailable", "events": []}


def test_flat_ics_minimal_projection_and_valid_empty():
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:synthetic
DTSTART;VALUE=DATE:20270409
SUMMARY:Movie\\, One
DESCRIPTION:SYNTHETIC_PRIVATE
URL:https://synthetic.invalid
END:VEVENT
BEGIN:VEVENT
DTSTART:20270410T092000Z
SUMMARY:TV Episode
END:VEVENT
BEGIN:VEVENT
DTSTART:20270410T092000Z
SUMMARY:TV Episode
END:VEVENT
BEGIN:VEVENT
DTSTART:20270410T092000Z
SUMMARY:Ignore
RRULE:FREQ=SECONDLY
END:VEVENT
END:VCALENDAR
"""
    rows = special.media_events("radarr", content)
    assert [(r["start"], r["title"]) for r in rows] == [
        ("2027-04-09", "Movie, One"), ("2027-04-10T09:20:00+00:00", "TV Episode")]
    assert all(set(r) == {"id", "start", "title"} and len(r["id"]) == 32 for r in rows)
    assert special.media_events("radarr", b"BEGIN:VCALENDAR\nEND:VCALENDAR\n") == []
    with pytest.raises(ValueError):
        special.media_events("radarr", b"not a calendar")


def test_selection_is_profile_local_and_refuses_links(tmp_path):
    a, b = tmp_path / "profile-a", tmp_path / "profile-b"
    a.mkdir()
    b.mkdir()
    assert special.selected_jobs(a) == special.selected_jobs(b) == []
    assert special.save_jobs(a, ["job-2", "job-1", "job-1"], lambda: True) == ["job-1", "job-2"]
    assert special.selected_jobs(a) == ["job-1", "job-2"] and special.selected_jobs(b) == []
    with pytest.raises(ValueError):
        special.save_jobs(a, [], lambda: False)
    assert special.selected_jobs(a) == ["job-1", "job-2"]
    (b / "plugin-data").symlink_to(a / "plugin-data", target_is_directory=True)
    with pytest.raises(OSError):
        special.selected_jobs(b)
    with pytest.raises(OSError):
        special.save_jobs(b, [], lambda: True)
    cron = b / "cron"
    cron.mkdir()
    (cron / "jobs.json").symlink_to(a / "plugin-data/icloud-caldav/automation-selection.json")
    with pytest.raises(OSError):
        special.scheduler_jobs(b, datetime.now(timezone.utc))


def test_month_is_inclusive_exclusive_in_client_zone():
    bounds = special.month_bounds("2027-04", "America/New_York")
    assert special.in_month("2027-04-01", bounds)
    assert not special.in_month("2027-05-01", bounds)
    assert not special.in_month("2027-04-01T03:59:59+00:00", bounds)
    assert special.in_month("2027-04-01T04:00:00+00:00", bounds)
    assert special.in_month("2027-05-01T03:59:59+00:00", bounds)
    assert not special.in_month("2027-05-01T04:00:00+00:00", bounds)


EMPTY_CALENDAR = b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"


@pytest.mark.parametrize("headers,body,outcome", [
    (b"200 OK\r\n", EMPTY_CALENDAR, "loaded"),
    (b"204 No Content\r\n", b"", "loaded"),
    (b"204 No Content\r\nContent-Length: 0\r\n", b"", "loaded"),
    (b"204 No Content\r\n", EMPTY_CALENDAR, "unavailable"),
    (b"204 No Content\r\nContent-Length: 0\r\n", b"x", "unavailable"),
    (b"204 No Content\r\nContent-Length: 1\r\n", b"", "unavailable"),
    (b"204 No Content\r\nTransfer-Encoding: chunked\r\n", b"0\r\n\r\n", "unavailable"),
    (b"204 No Content\r\nContent-Type: text/html\r\n", b"", "unavailable"),
    (b"200 OK\r\n", b"", "unavailable"),
    (b"200 OK\r\nContent-Type: text/html\r\n", b"<html>Login</html>", "unavailable"),
    (b"302 Found\r\nLocation: https://synthetic.invalid\r\n", EMPTY_CALENDAR, "unavailable"),
    (b"200 OK\r\nContent-Encoding: gzip\r\n", EMPTY_CALENDAR, "unavailable"),
    (b"200 OK\r\nContent-Length: 999999\r\n", EMPTY_CALENDAR, "unavailable"),
    (b"200 OK\r\nContent-Length: 100\r\n", EMPTY_CALENDAR, "unavailable"),
    (b"200 OK\r\nX-Slow: aaaaaaaaaaaaaaaaaaaaaaaa\r\n", EMPTY_CALENDAR, "timeout"),
])
def test_transport_is_one_bounded_get_with_no_followups(monkeypatch, headers, body, outcome):
    raw = "http://10.0.0.2:7878/feed/v3/calendar/Radarr.ics?apikey=SYNTHETIC_KEY&unmonitored=false"
    wire = bytearray(b"HTTP/1.1 " + headers + b"\r\n")
    calls = []
    tick = [0]

    class SyntheticSocket:
        def __init__(self, family, kind):
            assert (family, kind) == (special.socket.AF_INET, special.socket.SOCK_STREAM)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def settimeout(self, timeout):
            assert 0 < timeout <= 8
        def connect(self, endpoint):
            calls.append(endpoint)
        def sendall(self, data):
            calls.append(data)
        def recv_into(self, out):
            nonlocal body
            if not wire:
                if not body:
                    return 0
                wire.extend(body)  # Deliver separately so an empty header buffer is not evidence of EOF.
                body = b""
            count = 1 if outcome == "timeout" else min(len(wire), len(out))
            out[:count] = wire[:count]
            del wire[:count]
            tick[0] += 1
            return count

    monkeypatch.setattr(special.socket, "socket", SyntheticSocket)
    monkeypatch.setattr(special.time, "monotonic", lambda: tick[0])
    snapshot = lambda: ("a", True, True, raw)
    result = special.read_feed("radarr", snapshot, lambda: True)
    assert result == {"state": "loaded" if outcome == "loaded" else "unavailable", "events": []}
    assert len(calls) == 2 and calls[0] == ("10.0.0.2", 7878)
    assert calls[1].startswith(b"GET /feed/v3/calendar/Radarr.ics?apikey=SYNTHETIC_KEY&unmonitored=false HTTP/1.1\r\n")
    assert b"Accept-Encoding: identity\r\n" in calls[1]
    if outcome == "timeout":
        assert tick[0] == 8 and wire


@pytest.mark.parametrize("scheme,change_at,change", [
    ("http", None, None),
    ("https", None, None),
    ("http", "connect", "owner"),
    ("http", "connect", "http"),
    ("https", "connect", "owner"),
    ("https", "handshake", "owner"),
    ("https", "handshake", "consent"),
    ("https", "handshake", "url"),
])
def test_feed_rechecks_after_transport_setup_before_any_request_bytes(monkeypatch, scheme, change_at, change):
    raw = f"{scheme}://10.0.0.2/feed/v3/calendar/Sonarr.ics?apikey=SYNTHETIC_KEY"
    snapshot = ["a", True, True, raw]
    active = True
    stages, sent = [], []

    def stage(name):
        nonlocal active
        stages.append(name)
        if name != change_at:
            return
        if change == "owner":
            active = False
        else:
            snapshot[{"consent": 1, "http": 2, "url": 3}[change]] = False if change != "url" else raw + "2"

    class Transport:
        wire = bytearray(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

        def settimeout(self, _):
            pass

        def sendall(self, data):
            sent.append(data)

        def recv_into(self, out):
            count = min(len(out), len(self.wire))
            out[:count] = self.wire[:count]
            del self.wire[:count]
            return count

        def close(self):
            stages.append("tls closed")

    class Socket(Transport):
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            stages.append("tcp closed")

        def connect(self, _):
            stage("connect")

    class TLS:
        def __init__(self, protocol):
            assert protocol == special.ssl.PROTOCOL_TLS_CLIENT

        def load_default_certs(self):
            pass

        def wrap_socket(self, sock, server_hostname):
            assert isinstance(sock, Socket) and server_hostname == "10.0.0.2"
            stage("handshake")
            return Transport()

    monkeypatch.setattr(special.socket, "socket", Socket)
    monkeypatch.setattr(special.ssl, "SSLContext", TLS)
    result = special.read_feed("sonarr", lambda: tuple(snapshot), lambda: active)
    assert stages == (["connect", "handshake", "tls closed"] if scheme == "https" else ["connect"]) + ["tcp closed"]
    if change:
        assert not sent
        assert result == {"state": "unavailable", "events": []}
    else:
        assert len(sent) == 1 and sent[0].startswith(b"GET /feed/v3/calendar/Sonarr.ics?apikey=SYNTHETIC_KEY ")
        assert result == {"state": "loaded", "events": []}


def test_owner_routes_no_store_admission_and_stale_results(monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_special", Path(__file__).resolve().parents[1] / "dashboard/plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    from hermes_cli import plugin_host_http_auth

    owner = ["a" * 64]
    raw = "https://10.0.0.2:8989/feed/v5/calendar/Sonarr.ics?tags=1,2&apikey=SYNTHETIC_SECRET"
    enabled = [False]
    switch = [False]
    calls = []
    monkeypatch.setattr(plugin_host_http_auth, "admitted_for_sensitive_route", lambda r: r.headers.get("fixture") == "yes")
    monkeypatch.setattr(api, "owner_identity", lambda: owner[0])
    monkeypatch.setattr(api.special, "feed_snapshot", lambda vendor: ("a", enabled[0] and vendor == "sonarr", False, raw))

    def fetch(target, before_send):
        assert before_send()
        calls.append(target)
        if switch[0]:
            owner[0] = "b" * 64
        return (b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20270409T120000Z\n"
                b"SUMMARY:SYNTHETIC_SECRET\nEND:VEVENT\nEND:VCALENDAR\n")

    monkeypatch.setattr(api.special, "fetch_feed", fetch)
    app = FastAPI()
    app.include_router(api.router, prefix="/plugin")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            async def check(path, body, expected, admit=True):
                response = await client.post("/plugin/special/" + path, json=body, headers={"fixture": "yes"} if admit else {})
                assert response.status_code == expected
                assert response.headers["cache-control"] == "no-store"
                assert all(s not in response.text for s in ("SYNTHETIC_SECRET", "10.0.0.2", "apikey"))
                return response.json()

            payload = {"owner_id": owner[0], "month": "2027-04", "zone": "UTC"}
            await check("media/sonarr", payload, 401, False)
            await check("media/sonarr", {**payload, "extra": raw}, 400)
            await check("media/sonarr", {**payload, "zone": "x" * 600}, 413)
            await check("media/sonarr", {**payload, "owner_id": "c" * 64}, 409)
            assert (await check("media/sonarr", payload, 200))["state"] == "off"
            enabled[0] = True
            assert (await check("readiness", {"owner_id": owner[0]}, 200))["feeds"] == {"sonarr": "ready", "radarr": "off"}
            assert not calls
            result = await check("media/sonarr", payload, 200)
            assert result["state"] == "loaded" and result["events"] == []
            switch[0] = True
            assert (await check("media/sonarr", payload, 409))["error"] == "owner_changed"
            assert len(calls) == 2
    asyncio.run(run())
