"""Real wire-encoded loopback responses, never a pre-decoded MockTransport."""
import gzip
import threading
import zlib
from types import SimpleNamespace

import httpx
import pytest

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from icloud_caldav import client as client_module
from icloud_caldav.client import CalDAVClient, CalDAVError
from icloud_caldav.security import SecurityError


@contextmanager
def wire_server(routes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_PROPFIND(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, self.headers.get("Accept-Encoding")))
            status, headers, body = routes[self.path]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if headers.get("Transfer-Encoding") == "chunked":
                self.end_headers()
                for offset in range(0, len(body), 7):
                    chunk = body[offset:offset + 7]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_GET = do_PROPFIND

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = CalDAVClient(
        f"http://127.0.0.1:{server.server_port}", "synthetic", "synthetic",
        allow_loopback_http=True,
    )
    try:
        yield client, seen
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_gzip_207_decoded_once_with_response_metadata():
    body = b'<d:multistatus xmlns:d="DAV:"/>'
    wire = gzip.compress(body, mtime=0)
    headers = {"Content-Encoding": "gzip", "ETag": '"synthetic-etag"',
               "Content-Type": "application/xml", "X-Fixture": "retained"}
    with wire_server({"/": (207, headers, wire)}) as (client, seen):
        response = client._request("PROPFIND", client.base_url)
        assert response.status_code == 207
        assert response.content == body
        assert response.headers["etag"] == '"synthetic-etag"'
        assert response.headers["content-type"] == "application/xml"
        assert response.headers["x-fixture"] == "retained"
        assert str(response.url) == client.base_url
        assert response.request.method == "PROPFIND"
        assert response.is_closed
        assert "gzip" in seen[0][2]
        assert "content-encoding" not in response.headers
        assert response.headers["content-length"] == str(len(body))
        assert response.http_version == "HTTP/1.0"


@pytest.mark.parametrize("encoding,compress", [
    (None, lambda body: body),
    ("identity", lambda body: body),
    ("gzip", gzip.compress),
    ("deflate", zlib.compress),
    ("deflate", lambda body: zlib.compress(body, wbits=-zlib.MAX_WBITS)),
    ("gzip, deflate", lambda body: zlib.compress(gzip.compress(body))),
])
def test_supported_wire_encodings_and_uncompressed_get(encoding, compress):
    body = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    headers = {"ETag": 'W/"synthetic"', "Content-Type": "text/calendar"}
    if encoding:
        headers["Content-Encoding"] = encoding
    with wire_server({"/event.ics": (200, headers, compress(body))}) as (client, seen):
        resource = client.get(client.base_url + "event.ics")
        assert resource.data == body
        assert resource.etag == 'W/"synthetic"'
        assert resource.href == client.base_url + "event.ics"
        assert len(seen) == 1


def test_chunked_gzip_normalizes_only_wire_headers():
    body = b'<d:multistatus xmlns:d="DAV:"/>'
    headers = {"Content-Encoding": "gzip", "Transfer-Encoding": "chunked", "ETag": '"v1"'}
    with wire_server({"/": (207, headers, gzip.compress(body))}) as (client, _):
        response = client._request("PROPFIND", client.base_url)
        assert response.content == body
        assert response.headers["etag"] == '"v1"'
        assert response.headers["content-length"] == str(len(body))
        assert "content-encoding" not in response.headers
        assert "transfer-encoding" not in response.headers


@pytest.mark.parametrize("size", [2_000_000, 2_000_001])
def test_gzip_budget_counts_decoded_bytes_and_closes_stream(size):
    body = b"x" * size
    wire = gzip.compress(body)
    assert len(wire) < 10_000
    with wire_server({"/": (207, {"Content-Encoding": "gzip"}, wire)}) as (client, seen):
        streams = []
        client._client.event_hooks["response"].append(streams.append)
        if size > client_module._MAX_BODY:
            with pytest.raises(CalDAVError, match="^CalDAV response exceeded 2 MB$"):
                client._request("PROPFIND", client.base_url)
        else:
            assert client._request("PROPFIND", client.base_url).content == body
        assert len(seen) == 1
        assert len(streams) == 1 and streams[0].is_closed


@pytest.mark.parametrize("encoding,wire", [
    ("gzip", b"synthetic-private-content-not-gzip"),
    ("gzip", gzip.compress(b"synthetic")[:-8] + b"\x00" * 8),
    ("deflate", b"\x06synthetic-private-content-invalid-block"),
])
def test_malformed_compression_is_safe_error_and_closes_stream(encoding, wire):
    with wire_server({"/": (207, {"Content-Encoding": encoding}, wire)}) as (client, seen):
        streams = []
        client._client.event_hooks["response"].append(streams.append)
        with pytest.raises(CalDAVError) as error:
            client._request("PROPFIND", client.base_url)
        assert str(error.value) == "CalDAV request failed: DecodingError"
        assert isinstance(error.value.__cause__, httpx.DecodingError)
        assert len(seen) == 1
        assert len(streams) == 1 and streams[0].is_closed


def multistatus(prop, href="/"):
    return ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:response><d:href>' + href + '</d:href><d:propstat><d:prop>' + prop +
            '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
            '</d:response></d:multistatus>').encode()


def test_gzip_three_step_discovery_after_redirect_uses_effective_url():
    def encoded(prop, href="/"):
        return (207, {"Content-Encoding": "gzip"}, gzip.compress(multistatus(prop, href)))

    routes = {
        "/": (307, {"Location": "/root/", "Content-Encoding": "gzip"}, gzip.compress(b"redirect")),
        "/root/": encoded('<d:current-user-principal><d:href>principal/</d:href></d:current-user-principal>'),
        "/root/principal/": encoded('<c:calendar-home-set><d:href>home/</d:href></c:calendar-home-set>'),
        "/root/principal/home/": encoded('<d:resourcetype><c:calendar/></d:resourcetype>'
                                           '<d:displayname>Synthetic</d:displayname>', 'test/'),
    }
    with wire_server(routes) as (client, seen):
        rows = client.discover_calendars()
        identity = client.base_url + "root/principal/home/test/"
        assert rows == [{"id": identity, "url": identity, "name": "Synthetic"}]
        assert [path for _, path, _ in seen] == list(routes)


@pytest.mark.parametrize("method,target", [
    ("GET", "/denied/"),
    ("PROPFIND", "https://example.invalid/"),
    ("PROPFIND", "http://caldav.icloud.com/"),
])
def test_compressed_redirect_cannot_bypass_url_or_resource_policy(method, target):
    routes = {"/": (307, {"Location": target, "Content-Encoding": "gzip"}, gzip.compress(b"redirect"))}
    with wire_server(routes) as (client, seen):
        with pytest.raises(SecurityError):
            client._request(method, client.base_url)
        assert len(seen) == 1


def test_compressed_redirect_loop_is_bounded():
    routes = {"/": (307, {"Location": "/", "Content-Encoding": "gzip"}, gzip.compress(b"redirect"))}
    with wire_server(routes) as (client, seen):
        with pytest.raises(CalDAVError, match="too many CalDAV redirects"):
            client._request("PROPFIND", client.base_url)
        assert len(seen) == 4


def test_gzip_body_still_checks_deadline(monkeypatch):
    routes = {"/": (207, {"Content-Encoding": "gzip"}, gzip.compress(b"synthetic"))}
    with wire_server(routes) as (client, seen):
        streams = []
        client._client.event_hooks["response"].append(streams.append)
        clock = iter([0.0, 0.0, 31.0])
        monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        with pytest.raises(CalDAVError, match="^request time budget exceeded$"):
            client._request("PROPFIND", client.base_url)
        assert len(seen) == 1 and streams[0].is_closed


def test_request_budget_refuses_before_wire_io():
    with wire_server({}) as (client, seen):
        client._remaining_requests = 0
        with pytest.raises(CalDAVError, match="client request/time budget exceeded"):
            client._request("PROPFIND", client.base_url)
        assert seen == []
