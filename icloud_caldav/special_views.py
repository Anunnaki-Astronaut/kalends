"""Bounded, read-only projections for Kalends' optional calendar views."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import time
import unicodedata
from zoneinfo import ZoneInfo

from .runtime import PLUGIN_ID

VENDORS = ("sonarr", "radarr")
_FEED_SUFFIXES = {
    "sonarr": {("feed", "v5", "calendar", "Sonarr.ics"),
               ("feed", "v3", "calendar", "Sonarr.ics"),
               ("feed", "v3", "calendar", "sonarr.ics")},
    "radarr": {("feed", "v3", "calendar", "Radarr.ics")},
}
_TAG_IDS = re.compile(r"[0-9]{1,10}(?:,[0-9]{1,10}){0,31}")
_RELEASE_TYPES = {"cinemaRelease", "digitalRelease", "physicalRelease"}
PRIVATE = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
MAX_ICS = 512 * 1024
MAX_EVENTS = 1000
MAX_JOBS = 250
MAX_SELECTION = 100
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
_PREFS = "automation-selection.json"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _title(value, limit=200):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    return " ".join("".join(
        " " if unicodedata.category(c).startswith("C") else c for c in value
    ).split())[:limit]


@contextmanager
def _directory(home, parts, create=False):
    """Follow the host's profile anchor, not links below it."""
    fd = os.open(home, _DIR)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read_json(directory, name, cap, private=False):
    fd = os.open(name, _FILE, dir_fd=directory)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_size > cap or
                (private and (st.st_mode & 0o077 or st.st_uid != os.geteuid() or st.st_nlink != 1))):
            raise ValueError()
        data = f.read(cap + 1)
    if len(data) > cap:
        raise ValueError()
    return json.loads(data.decode("utf-8-sig"))


def selection(value):
    if (not isinstance(value, list) or len(value) > MAX_SELECTION or
            any(not isinstance(v, str) or not _JOB_ID.fullmatch(v) for v in value)):
        raise ValueError()
    return sorted(set(value))


def selected_jobs(home):
    try:
        with _directory(home, ("plugin-data", PLUGIN_ID)) as directory:
            data = _read_json(directory, _PREFS, 12_000, private=True)
        if not isinstance(data, dict) or set(data) != {"version", "ids"} or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError()
        return selection(data["ids"])
    except FileNotFoundError:
        return []


def save_jobs(home, ids, current):
    ids = selection(ids)
    with _directory(home, ("plugin-data", PLUGIN_ID), create=True) as directory:
        try:
            _read_json(directory, _PREFS, 12_000, private=True)
        except FileNotFoundError:
            pass
        name = ".automation-" + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "ids": ids}, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            if not current():
                raise ValueError()
            os.replace(name, _PREFS, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    return ids


def scheduler_jobs(home, now):
    # Reference cron.jobs.load_jobs calls ensure_dirs and can save repairs.
    # list_jobs additionally opens executions.db; the private peek is unbounded.
    # The scheduler atomically replaces this canonical per-profile snapshot.
    try:
        with _directory(home, ("cron",)) as directory:
            data = _read_json(directory, "jobs.json", 2 * 1024 * 1024)
    except FileNotFoundError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
        raise ValueError()
    rows = []
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        ident = job.get("id")
        if not isinstance(ident, str) or not _JOB_ID.fullmatch(ident) or ident in seen:
            continue
        seen.add(ident)
        state = job.get("state")
        state = state.strip() if isinstance(state, str) else ""
        # Hermes treats a pause timestamp as an independent gate even if enabled is stale.
        enabled = (job.get("enabled") is True and not job.get("paused_at")
                   and state not in ("paused", "completed", "error"))
        stamp = job.get("next_run_at")
        start = None
        if (enabled and isinstance(stamp, str) and len(stamp) <= 48 and "T" in stamp
                and stamp != job.get("manual_run_at")):
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                # Canonical writes are aware. Do not guess a legacy naive wall time.
                if dt.tzinfo is not None and dt.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                    start = dt.isoformat()
            except (ValueError, OverflowError):
                pass
        rows.append({"id": ident, "title": _title(job.get("name")) or "Unnamed automation",
                     "enabled": enabled, "start": start})
    return rows


def automation_events(home, now, bounds):
    ids = set(selected_jobs(home))
    if not ids:
        return {"state": "off", "events": []}
    return {"state": "loaded", "events": [
        {key: row[key] for key in ("id", "title", "start")}
        for row in scheduler_jobs(home, now)
        if row["id"] in ids and row["start"] and in_month(row["start"], bounds)
    ]}


def feed_snapshot(vendor):
    from agent.secret_scope import get_secret_str
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home

    home = str(get_hermes_home())
    cfg = (((load_config_readonly().get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}).get("settings") or {}
    consent = cfg.get(vendor + "_consent") is True
    return home, consent, cfg.get(vendor + "_allow_http") is True, (
        get_secret_str("KALENDS_" + vendor.upper() + "_ICAL_URL") if consent else None)


def feed_target(vendor, raw, allow_http):
    if vendor not in VENDORS or not isinstance(raw, str) or len(raw) > 1024:
        raise ValueError()
    match = re.fullmatch(
        r"(https?)://([0-9.]{7,15})(?::([1-9][0-9]{0,4}))?(/[A-Za-z0-9._/-]+)(?:\?([A-Za-z0-9_,=&-]+))?", raw)
    if not match:
        raise ValueError()
    scheme, host, port, path, query = match.groups()
    address = ipaddress.IPv4Address(host)
    if not any(address in network for network in PRIVATE) or (scheme == "http" and allow_http is not True):
        raise ValueError()
    if port and int(port) > 65535:
        raise ValueError()
    segments = path[1:].split("/")
    if (len(segments) > 12 or tuple(segments[-4:]) not in _FEED_SUFFIXES[vendor] or
            any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", s) for s in segments)):
        raise ValueError()
    params = {}
    release_types = set()
    for param in query.split("&") if query else ():
        name, _, value = param.partition("=")
        if vendor == "radarr" and name == "releaseTypes":
            if value not in _RELEASE_TYPES or value in release_types:
                raise ValueError()
            release_types.add(value)
            continue
        if name in params or not (
            (name == "apikey" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)) or
            (name == "tags" and _TAG_IDS.fullmatch(value)) or
            (name in {"unmonitored", "asAllDay"} and value in {"true", "false"}) or
            (vendor == "sonarr" and name == "premieresOnly" and value in {"true", "false"})
        ):
            raise ValueError()
        params[name] = value
    return scheme, host, int(port or (443 if scheme == "https" else 80)), (
        path + ("?" + query if query else "")), params.get("apikey")


def feed_state(vendor, snapshot):
    _, consent, http, raw = snapshot
    if not consent:
        return "off"
    if not raw:
        return "not_configured"
    try:
        feed_target(vendor, raw, http)
    except ValueError:
        return "unavailable"
    return "ready"


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, remaining):
        self.sock, self.remaining = sock, remaining
        self.budget = MAX_ICS + 128 * 1024   # Headers and chunk framing, too.

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.budget <= 0:
            raise ValueError()
        self.sock.settimeout(self.remaining())
        count = self.sock.recv_into(memoryview(buffer)[:self.budget])
        self.budget -= count
        return count

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError()
        return io.BufferedReader(self)


def fetch_feed(target, before_send):
    """Return calendar bytes, or None only for a verified empty HTTP 204."""
    scheme, host, port, resource, _ = target
    deadline = time.monotonic() + 8

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        return left

    # AF_INET + validated numeric address: no resolver, proxy, cookies, redirects
    # or URL-aware logging. TLS checks the IP certificate SAN.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(remaining())
        raw.connect((host, port))
        transport = raw
        try:
            if scheme == "https":
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.load_default_certs()
                raw.settimeout(remaining())
                transport = ctx.wrap_socket(raw, server_hostname=host)
            transport.settimeout(remaining())
            # TCP and TLS can block while owner or feed settings change. This
            # final local check is not atomic with the subsequent send.
            if not before_send():
                raise ValueError()
            transport.sendall((
                f"GET {resource} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Accept: text/calendar\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
            ).encode("ascii"))
            with http.client.HTTPResponse(_DeadlineReader(transport, remaining), method="GET") as response:
                response.begin()
                encodings = response.headers.get_all("Content-Encoding", [])
                transfers = response.headers.get_all("Transfer-Encoding", [])
                lengths = response.headers.get_all("Content-Length", [])
                if (response.status not in (200, 204) or
                        any(s.split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}
                            for s in response.headers.get_all("Content-Type", [])) or
                        any(s.lower().strip() != "identity" for s in encodings) or
                        (transfers and ([s.lower().strip() for s in transfers] != ["chunked"] or lengths)) or
                        (lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]) or int(lengths[0]) > MAX_ICS))):
                    raise ValueError()
                if response.status == 204:
                    # HTTPResponse.read() treats 204 as length zero regardless of what's
                    # on the wire. Check its buffered stream through EOF instead.
                    if transfers or (lengths and int(lengths[0]) != 0) or response.fp.read(1):
                        raise ValueError()
                    remaining()
                    return None
                data = response.read(MAX_ICS + 1)
                remaining()
                if len(data) > MAX_ICS or (lengths and len(data) != int(lengths[0])):
                    raise ValueError()
                return data
        finally:
            if transport is not raw:
                transport.close()


def _start(field, local_zone):
    params, value = field
    if params in ("", ";VALUE=DATE") and re.fullmatch(r"[0-9]{8}", value):
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z?", value):
        raise ValueError()
    if params in ("", ";VALUE=DATE-TIME"):
        tz = timezone.utc if value.endswith("Z") else local_zone
    elif not value.endswith("Z") and params.upper().startswith(";TZID="):
        tz = calendar_zone(params[6:])
    else:
        raise ValueError()
    return datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=tz).isoformat()


def media_events(vendor, data, local_zone=timezone.utc, secret=None):
    if len(data) > MAX_ICS:
        raise ValueError()
    # Unfold bytes before UTF-8 decode: RFC 5545 folds may split a code point.
    lines = re.sub(rb"\r?\n[ \t]", b"", data).decode("utf-8-sig").strip("\r\n").split("\n")
    if len(lines) > 16000 or lines[0].rstrip("\r").upper() != "BEGIN:VCALENDAR" or lines[-1].rstrip("\r").upper() != "END:VCALENDAR":
        raise ValueError()
    stack, fields, result = [], {}, {}
    count = 0
    bad = False
    for line in lines[1:-1]:
        head, colon, value = line.rstrip("\r").partition(":")
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "BEGIN" and colon:
            component = value.upper()
            if len(stack) >= 4 or not re.fullmatch(r"[A-Z0-9-]{1,32}", component) or component == "VCALENDAR":
                raise ValueError()
            stack.append(component)
            if stack == ["VEVENT"]:
                count += 1
                if count > MAX_EVENTS:
                    raise ValueError()
                fields, bad = {}, False
        elif name == "END" and colon:
            if not stack or stack[-1] != value.upper():
                raise ValueError()
            if stack == ["VEVENT"] and not bad:
                try:
                    start = _start(fields["DTSTART"], local_zone)
                    summary = re.sub(r"\\([nN\\;,])", lambda m: " " if m[1] in "nN" else m[1], fields["SUMMARY"][1])
                    title = _title(summary)
                    if title and not (secret and secret in summary) and not re.search(r"[<>]|://", summary):
                        ident = hashlib.sha256(json.dumps([vendor, start, title]).encode()).hexdigest()[:32]
                        result[ident] = {"id": ident, "start": start, "title": title}
                except (KeyError, ValueError, OverflowError):
                    pass
            stack.pop()
        elif stack == ["VEVENT"]:
            if len(line) > 8192 or name in {"RRULE", "RDATE", "EXRULE", "EXDATE", "RECURRENCE-ID"}:
                bad = True
            if name in {"DTSTART", "SUMMARY"}:
                if not colon or name in fields or len(value) > 2048:
                    bad = True
                fields[name] = (";" + params if params else "", value)
    if stack:
        raise ValueError()
    return list(result.values())


def calendar_zone(zone):
    if (not isinstance(zone, str) or len(zone) > 64 or
            not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", zone) or
            zone.split("/")[0] in {"localtime", "posix", "right"}):
        raise ValueError()
    return ZoneInfo(zone)


def month_bounds(month, zone):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError()
    y, m = map(int, month.split("-"))
    if not 1900 <= y <= 2200:
        raise ValueError()
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1), calendar_zone(zone)


def in_month(start, bounds):
    first, end, zone = bounds
    day = date.fromisoformat(start) if len(start) == 10 else datetime.fromisoformat(start).astimezone(zone).date()
    return first <= day < end


def read_feed(vendor, snapshot, current, fetch=None, bounds=None):
    unavailable = {"state": "unavailable", "events": []}
    try:
        initial = snapshot()
        def still_current():
            return current() and snapshot() == initial

        state = feed_state(vendor, initial)
        if not still_current():
            return unavailable
        if state != "ready":
            return {"state": state, "events": []}
        target = feed_target(vendor, initial[3], initial[2])
        data = (fetch or fetch_feed)(target, still_current)
        if not still_current():
            return unavailable
        events = [] if data is None else media_events(vendor, data, bounds[2] if bounds else timezone.utc, target[-1])
        if bounds:
            events = [row for row in events if in_month(row["start"], bounds)]
        if still_current():
            return {"state": "loaded", "events": events}
    except Exception:
        # Never pass provider, credential, parser, or event details to logs/errors.
        pass
    return unavailable
