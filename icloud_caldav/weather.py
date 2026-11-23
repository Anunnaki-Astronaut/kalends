"""Optional, deliberately small Pirate Weather projection. No CalDAV credentials."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import socket
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

CONDITIONS = frozenset({
    "clear-day", "clear-night", "rain", "snow", "sleet", "wind",
    "fog", "cloudy", "partly-cloudy-day", "partly-cloudy-night",
})
_lock = threading.RLock()
_cache = {}
_observed = {}
_inflight = set()


def _observe(scope):
    profile = hashlib.sha256(str(scope[0]).encode()).digest()
    identity = hashlib.sha256(json.dumps(scope).encode()).digest()
    with _lock:
        previous = _observed.get(profile)
        if previous != identity:
            _cache.pop(previous, None)
        if len(_observed) >= 32 and profile not in _observed:
            _observed.clear()
            _cache.clear()
        _observed[profile] = identity
    return identity


def snapshot():
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home
    from agent.secret_scope import get_secret_str
    raw = load_config_readonly()
    cfg = (((raw.get("plugins") or {}).get("entries") or {}).get("icloud-caldav") or {}).get("settings") or {}
    result = (str(get_hermes_home()), cfg.get("weather_consent") is True,
            cfg.get("forecast_latitude"), cfg.get("forecast_longitude"),
            get_secret_str("KALENDS_PIRATE_WEATHER_KEY") if cfg.get("weather_consent") is True else None)
    _observe(result)
    return result


def _coordinates(lat, lon):
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError()
    lat, lon = float(lat), float(lon)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError()
    return lat, lon


def _fetch(key, lat, lon, connection=http.client.HTTPSConnection):
    # http.client does not emit request URL INFO logs, use proxies, or follow
    # redirects. The documented apikey header permits a dummy path key.
    conn = connection("api.pirateweather.net", timeout=5)
    if hasattr(conn, "set_debuglevel"):
        conn.set_debuglevel(0)
    transport_socket = [None]
    # A wall-clock socket shutdown also bounds slow-drip headers/body, rather
    # than restarting a five-second inactivity allowance on every byte.
    def expire():
        sock = transport_socket[0] or getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    deadline = threading.Timer(8, expire)
    deadline.daemon = True
    deadline.start()
    try:
        conn.request("GET", f"/forecast/0/{lat},{lon}?exclude=currently,minutely,hourly,alerts",
                     headers={"apikey": key, "Accept": "application/json"})
        transport_socket[0] = getattr(conn, "sock", None)
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError()
        body = response.read(131073)
        if len(body) > 131072:
            raise ValueError()
        raw = json.loads(body)
        zone = ZoneInfo(raw["timezone"])
        rows = raw["daily"]["data"]
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError()
        days = {}
        flags = raw.get("flags")
        units = flags.get("units") if isinstance(flags, dict) else None
        unit = "F" if units == "us" else "C" if units in ("ca", "uk", "uk2", "si") else None
        def bounded(value, low, high):
            return (not isinstance(value, bool) and isinstance(value, (int, float))
                    and low <= value <= high and math.isfinite(value))
        for row in rows:
            stamp, code = row.get("time"), row.get("icon")
            if isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
                continue
            if code in CONDITIONS:
                day = datetime.fromtimestamp(stamp, zone).date().isoformat()
                detail = {"date": day, "condition": code}
                if unit:
                    bounds = (-130, 140) if unit == "F" else (-90, 60)
                    for source, target in (("temperatureHigh", "high"), ("temperatureLow", "low")):
                        value = row.get(source)
                        if bounded(value, *bounds):
                            detail[target] = round(value)
                    if "high" in detail or "low" in detail:
                        detail["temperature_unit"] = unit
                probability = row.get("precipProbability")
                if bounded(probability, 0, 1):
                    detail["precipitation_percent"] = round(probability * 100)
                days[day] = detail
        return [detail for day, detail in sorted(days.items())]
    finally:
        deadline.cancel()
        conn.close()


def forecast(read_snapshot=snapshot, fetch=_fetch, now=time.monotonic):
    """Memory-only TTL/backoff partitioned by profile AND all consent inputs."""
    disabled = {"ok": True, "days": [], "weather_state": "off"}
    unavailable = {"ok": True, "days": [], "weather_state": "unavailable"}
    setup = {"ok": True, "days": [], "weather_state": "setup_needed"}
    try:
        initial = read_snapshot()
        identity = _observe(initial)
        profile, consent, lat, lon, key = initial
        if consent is not True:
            return disabled
        try:
            lat, lon = _coordinates(lat, lon)
        except (TypeError, ValueError, OverflowError):
            return setup if read_snapshot() == initial else disabled
        if not isinstance(key, str) or not key or len(key) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            return setup if read_snapshot() == initial else disabled
        with _lock:
            if read_snapshot() != initial:
                return disabled
            cached = _cache.get(identity)
            if cached and cached[0] > now():
                return cached[1]
            if identity in _inflight:
                return unavailable
            _inflight.add(identity)
        try:
            days = fetch(key, lat, lon)
            result, ttl = {"ok": True, "days": days, "weather_state": "available"}, 1800
        except Exception:
            # Never propagate vendor bodies, URLs, location or credentials.
            result, ttl = unavailable, 60
        finally:
            with _lock:
                _inflight.discard(identity)
        with _lock:
            if read_snapshot() != initial:
                return disabled
            if len(_cache) >= 32:
                _cache.clear()
            _cache[identity] = (now() + ttl, result)
            return result
    except Exception:
        return unavailable
