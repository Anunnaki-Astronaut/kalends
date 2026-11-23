from __future__ import annotations

import xml.etree.ElementTree as ET
import time
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .exceptions import (
    AuthenticationError, CalDAVError, ConnectivityError, PermissionDenied,
    ResponseFormatError, WorkBudgetError,
)
from .security import SecurityError, same_authorized_origin, validate_caldav_url, canonical_url, member_url

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CALDAV, "a": "http://apple.com/ns/ical/"}
_MAX_BODY = 2_000_000


def _calendar_color(value: str) -> str | None:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
        return None
    # Nearly transparent source swatches are unusable; let the UI use its fallback.
    if len(value) == 9 and int(value[-2:], 16) < 128:
        return None
    return value.lower()


class IndeterminateError(CalDAVError):
    def __init__(self, href):
        super().__init__("mutation outcome indeterminate; reconcile by reading the resource; do not retry with a new operation_id")
        self.href = href


class NotFoundError(CalDAVError):
    pass


class ConflictError(CalDAVError):
    pass


@dataclass(frozen=True)
class Resource:
    href: str
    etag: str
    data: bytes


def ambiguous_write(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(self, target, *args, **kwargs):
        href = self._resource_url(target)
        if fn.__name__ == 'create':
            href = href.rstrip('/') + '/' + quote(args[0], safe='') + '.ics'
            self._resource_url(href)
        try:
            return fn(self, target, *args, **kwargs)
        except ConflictError:
            raise
        except Exception as exc:
            raise IndeterminateError(href) from exc
    return wrapped


class CalDAVClient:
    def __init__(self, base_url: str, username: str, password: str, *, allow_loopback_http: bool = False, timeout: float = 20.0):
        from . import __version__

        self.allowed = validate_caldav_url(base_url, allow_loopback_http=allow_loopback_http)
        self.base_url = self.allowed.url + "/"
        self.allow_loopback_http = allow_loopback_http
        self._deadline = time.monotonic() + 60
        self._remaining_requests = 16
        self._timeout = timeout
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password), verify=True, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={"User-Agent": f"Hermes-iCloud-CalDAV/{__version__}"},
        )

    def close(self):
        self._client.close()

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        deadline = min(self._deadline, time.monotonic() + 30)
        current = canonical_url(url)
        for _ in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._remaining_requests <= 0:
                raise WorkBudgetError("client request/time budget exceeded")
            self._remaining_requests -= 1
            kwargs["timeout"] = min(self._timeout, remaining)
            same_authorized_origin(current, self.allowed, allow_loopback_http=self.allow_loopback_http)
            try:
                with self._client.stream(method, current, **kwargs) as streamed:
                    body = bytearray()
                    for chunk in streamed.iter_bytes():
                        body.extend(chunk)
                        if time.monotonic()>deadline:
                            raise WorkBudgetError("request time budget exceeded")
                        if len(body) > _MAX_BODY:
                            raise WorkBudgetError("CalDAV response exceeded 2 MB")
                    # iter_bytes() already decoded the wire body. Reusing its
                    # encoding/framing headers would decode it a second time.
                    headers = streamed.headers.copy()
                    for name in ("content-encoding", "content-length", "transfer-encoding"):
                        headers.pop(name, None)
                    response = httpx.Response(
                        streamed.status_code,
                        headers=headers,
                        content=bytes(body),
                        request=streamed.request,
                        extensions=streamed.extensions,
                    )
            except CalDAVError:
                raise
            except httpx.DecodingError as exc:
                raise ResponseFormatError("CalDAV request failed: DecodingError") from exc
            except httpx.TransportError as exc:
                raise ConnectivityError("CalDAV transport failed") from exc
            except httpx.HTTPError as exc:
                raise CalDAVError(f"CalDAV request failed: {type(exc).__name__}") from exc
            if response.status_code == 401:
                raise AuthenticationError("calendar authentication failed")
            if response.status_code == 403:
                raise PermissionDenied("calendar access denied")
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                raise ResponseFormatError("CalDAV redirect had no location")
            target = canonical_url(location, current)
            # Resource operations never follow redirects across collection/origin boundaries.
            if method != "PROPFIND":
                raise SecurityError("resource redirect refused")
            current = target
        raise WorkBudgetError("too many CalDAV redirects")

    @staticmethod
    def _xml(data: bytes) -> ET.Element:
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ResponseFormatError("unsafe XML response")
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise ResponseFormatError("invalid CalDAV XML response") from exc

    def _propfind(self, url: str, body: bytes, depth: str) -> tuple[ET.Element, str]:
        response = self._request("PROPFIND", url, content=body, headers={"Depth": depth, "Content-Type": "application/xml"})
        if response.status_code not in {200, 207}:
            raise CalDAVError(f"calendar discovery failed ({response.status_code})")
        return self._xml(response.content), str(response.url)

    def _calendars_from_multistatus(self, root: ET.Element, context_url: str) -> list[dict[str, str]]:
        result = []
        for item in root.findall("d:response", NS):
            # Properties can be split across successful and failed propstats.
            prop = ET.Element(f"{{{DAV}}}prop")
            for propstat in item.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS).split()
                if len(status) < 2 or not re.fullmatch(r"HTTP/\d+\.\d+", status[0]) or not re.fullmatch(r"2\d\d", status[1]):
                    continue
                successful = propstat.find("d:prop", NS)
                if successful is not None:
                    prop.extend(successful)
            href_node = item.find("d:href", NS)
            if href_node is None or not href_node.text:
                continue
            resource_type = prop.find("d:resourcetype", NS)
            if resource_type is None or resource_type.find("c:calendar", NS) is None:
                continue
            components = prop.find("c:supported-calendar-component-set", NS)
            if components is not None and not any(c.attrib.get("name") == "VEVENT" for c in components):
                continue
            href = self._resource_url(canonical_url(href_node.text, context_url))
            if not href.endswith("/"):
                raise SecurityError("calendar identity must be a collection")
            name = prop.findtext("d:displayname", default=href.rstrip("/").rsplit("/", 1)[-1], namespaces=NS)
            row = {"id": href, "name": name, "url": href}
            color = _calendar_color(prop.findtext("a:calendar-color", default="", namespaces=NS))
            if color is not None:
                row["color"] = color
            result.append(row)
        if len(result)>100: raise WorkBudgetError("calendar discovery budget exceeded")
        return sorted(result, key=lambda row: (row["name"].casefold(), row["id"]))

    def discover_calendars(self) -> list[dict[str, str]]:
        initial = b'''<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/"><d:prop><d:current-user-principal/><c:calendar-home-set/><d:displayname/><d:resourcetype/><c:supported-calendar-component-set/><a:calendar-color/></d:prop></d:propfind>'''
        root, effective = self._propfind(self.base_url, initial, "0")
        if direct := self._calendars_from_multistatus(root, effective):
            return direct
        principal_href = root.findtext(".//d:current-user-principal/d:href", default="", namespaces=NS)
        principal_url = self._resource_url(canonical_url(principal_href or effective, effective))
        principal, effective = self._propfind(
            principal_url,
            b'''<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>''',
            "0",
        )
        home_href = principal.findtext(".//c:calendar-home-set/d:href", default="", namespaces=NS)
        if not home_href:
            # Some servers expose calendars directly under the initial collection.
            fallback, effective = self._propfind(self.base_url, initial, "1")
            calendars = self._calendars_from_multistatus(fallback, effective)
            if calendars:
                return calendars
            raise CalDAVError("calendar discovery returned no calendar home")
        home_url = self._resource_url(canonical_url(home_href, effective))
        root, effective = self._propfind(home_url, initial, "1")
        calendars = self._calendars_from_multistatus(root, effective)
        if not calendars:
            raise CalDAVError("calendar discovery returned no VEVENT calendars")
        return calendars

    def _resource_url(self, href: str) -> str:
        url = canonical_url(href, self.base_url)
        same_authorized_origin(url, self.allowed, allow_loopback_http=self.allow_loopback_http)
        return url

    def query(self, calendar_url: str, start: datetime, end: datetime) -> list[Resource]:
        url = self._resource_url(calendar_url)
        start_utc = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end_utc = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = f'''<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{start_utc}" end="{end_utc}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'''.encode()
        response = self._request("REPORT", url, content=body, headers={"Depth": "1", "Content-Type": "application/xml"})
        if response.status_code not in {200, 207}:
            raise CalDAVError(f"calendar query failed ({response.status_code})")
        resources = []
        for item in self._xml(response.content).findall("d:response", NS):
            href = item.findtext("d:href", default="", namespaces=NS)
            etag = item.findtext("d:propstat/d:prop/d:getetag", default="", namespaces=NS)
            data = item.findtext("d:propstat/d:prop/c:calendar-data", default="", namespaces=NS)
            if href and etag and data:
                resources.append(Resource(href=member_url(url, self._resource_url(canonical_url(href, str(response.url)))), etag=etag, data=data.encode()))
        if len(resources) > 100:
            raise WorkBudgetError("query resource budget exceeded")
        return resources

    def get(self, href: str) -> Resource:
        response = self._request("GET", self._resource_url(href), headers={"Accept": "text/calendar"})
        if response.status_code == 404:
            raise NotFoundError("calendar event not found")
        if response.status_code != 200:
            raise CalDAVError(f"event read failed ({response.status_code})")
        etag = response.headers.get("etag")
        if not etag:
            raise ResponseFormatError("event read returned no ETag")
        return Resource(href=self._resource_url(href), etag=etag, data=response.content)

    @ambiguous_write
    def create(self, calendar_url: str, uid: str, data: bytes) -> Resource:
        href = self._resource_url(calendar_url).rstrip("/") + "/" + quote(uid, safe="") + ".ics"
        response = self._request("PUT", self._resource_url(href), content=data, headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if response.status_code == 412:
            raise ConflictError("event UID already exists; create was not retried")
        if response.status_code not in {201, 204}:
            raise CalDAVError(f"event create failed ({response.status_code})")
        result = self.get(href)
        from .ics import semantically_equal
        if not semantically_equal(data, result.data):
            raise IndeterminateError(href)
        return result

    @ambiguous_write
    def update(self, href: str, etag: str, data: bytes) -> Resource:
        if not etag:
            raise ValueError("ETag is required for update")
        response = self._request("PUT", self._resource_url(href), content=data, headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": etag})
        if response.status_code == 412:
            raise ConflictError("event changed on the server; no overwrite occurred")
        if response.status_code not in {200, 201, 204}:
            raise CalDAVError(f"event update failed ({response.status_code})")
        result = self.get(href)
        from .ics import semantically_equal
        if not semantically_equal(data, result.data):
            raise IndeterminateError(href)
        return result

    @ambiguous_write
    def delete(self, href: str, etag: str) -> None:
        if not etag:
            raise ValueError("ETag is required for delete")
        response = self._request("DELETE", self._resource_url(href), headers={"If-Match": etag})
        if response.status_code == 412:
            raise ConflictError("event changed on the server; it was not deleted")
        if response.status_code not in {200, 204}:
            raise CalDAVError(f"event delete failed ({response.status_code})")
        verify = self._request("GET", self._resource_url(href), headers={"Accept": "text/calendar"})
        if verify.status_code != 404:
            raise CalDAVError("delete could not be verified")
