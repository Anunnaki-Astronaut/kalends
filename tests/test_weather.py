"""Synthetic weather transport only; never contacts a vendor."""
import json
from datetime import datetime, timezone

import pytest

from icloud_caldav import weather


@pytest.fixture(autouse=True)
def empty_cache():
    weather._cache.clear()
    weather._observed.clear()
    weather._inflight.clear()
    yield
    weather._cache.clear()
    weather._observed.clear()
    weather._inflight.clear()


def scope(**changes):
    data = dict(profile="synthetic-profile", consent=True, lat="12.5", lon="-23.5", key="synthetic-key")
    data.update(changes)
    return tuple(data.values())


@pytest.mark.parametrize("changes", [
    {"consent": False}, {"consent": "true"}, {"lat": "nan"},
    {"lon": "inf"}, {"lat": 91}, {"lon": -181}, {"lat": True},
    {"lat": ""}, {"key": ""}, {"key": "bad\r\nheader"},
])
def test_disabled_invalid_never_fetch(changes):
    def fetch(*args):
        pytest.fail("must not fetch")
    assert weather.forecast(lambda: scope(**changes), fetch)["days"] == []


def test_ttl_scope_and_backoff():
    calls = []
    clock = [100]
    current = [scope()]
    def fetch(*args):
        calls.append(args)
        return [{"date": "2026-11-03", "condition": "rain"}]
    read = lambda: current[0]
    for _ in range(2):
        assert weather.forecast(read, fetch, lambda: clock[0])["days"]
    assert len(calls) == 1
    for changed in (scope(profile="other"), scope(key="replacement"), scope(lat="13")):
        current[0] = changed
        weather.forecast(read, fetch, lambda: clock[0])
    assert len(calls) == 4
    current[0] = scope(consent=False)
    assert weather.forecast(read, fetch)["weather_state"] == "off"
    current[0] = scope(key="failure")
    def fail(*args):
        calls.append(args)
        raise ValueError("PRIVATE VENDOR LOCATION KEY")
    assert "PRIVATE" not in str(weather.forecast(read, fail, lambda: clock[0]))
    weather.forecast(read, fail, lambda: clock[0])
    assert len(calls) == 5
    clock[0] += 61
    weather.forecast(read, fail, lambda: clock[0])
    assert len(calls) == 6


def test_changed_consent_during_fetch_discards_result():
    current = [scope()]
    def fetch(*args):
        current[0] = scope(consent=False)
        return [{"date": "2026-11-03", "condition": "rain"}]
    assert weather.forecast(lambda: current[0], fetch)["days"] == []
    assert not weather._cache


def test_snapshot_without_consent_never_reads_forecast_secret(monkeypatch):
    import sys
    import types
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"plugins": {"entries": {"icloud-caldav": {"settings": {"weather_consent": False}}}}}
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "synthetic-profile"
    secret = types.ModuleType("agent.secret_scope")
    secret.get_secret_str = lambda key: pytest.fail("No secret lookup without consent")
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret)
    assert weather.snapshot() == ("synthetic-profile", False, None, None, None)


def test_header_auth_fixed_host_timezone_and_allowlist(caplog):
    caplog.set_level("DEBUG")
    calls = []
    stamp = datetime(2026, 11, 4, 1, tzinfo=timezone.utc).timestamp()
    data = {"timezone": "America/New_York", "daily": {"data": [
        {"time": stamp, "icon": "rain", "summary": "PRIVATE"},
        {"time": stamp + 86400, "icon": "untrusted"},
    ]}}
    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.pirateweather.net" and timeout == 5
        def request(self, method, path, headers):
            assert method == "GET"
            assert "synthetic-key" not in path
            assert path.startswith("/forecast/0/")
            assert headers["apikey"] == "synthetic-key"
            calls.append(path)
        def getresponse(self):
            return self
        status = 200
        def read(self, limit):
            assert limit == 131073
            return json.dumps(data).encode()
        def close(self):
            pass
    assert weather._fetch("synthetic-key", 12.5, -23.5, Connection) == [
        {"date": "2026-11-03", "condition": "rain"}]
    assert len(calls) == 1
    assert "synthetic-key" not in caplog.text and "12.5" not in caplog.text


@pytest.mark.parametrize("zone,pairs", [
    ("Pacific/Auckland", [(1790337600, "2026-09-26"), (1790424000, "2026-09-27"), (1790506800, "2026-09-28")]),
    ("America/New_York", [(1793419200, "2026-10-31"), (1793505600, "2026-11-01"), (1793595600, "2026-11-02")]),
])
def test_dst_available_dates_and_debug_nondisclosure(zone, pairs, caplog):
    caplog.set_level("DEBUG")
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        status = 200
        def read(self, limit):
            return json.dumps({"timezone": zone, "daily": {"data": [
                {"time": stamp, "icon": "rain"} for stamp, _ in pairs
            ]}}).encode()
        def close(self): pass
    assert weather._fetch("synthetic-key", 12.5, -23.5, Connection) == [
        {"date": date, "condition": "rain"} for _, date in pairs
    ]
    def fail(*args):
        raise ValueError("https://api.pirateweather.net/PRIVATE synthetic-key 12.5 -23.5")
    assert weather.forecast(lambda: scope(), fail)["weather_state"] == "unavailable"
    for forbidden in ("synthetic-key", "12.5", "-23.5", "PRIVATE", "api.pirateweather.net"):
        assert forbidden not in caplog.text


@pytest.mark.parametrize("status,body", [(302, b""), (429, b"PRIVATE"), (200, b"x" * 131073), (200, b'{"timezone":"Invalid"}')])
def test_redirect_busy_oversized_and_malformed_are_refused(status, body):
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        def read(self, limit): return body
        def close(self): pass
    Connection.status = status
    with pytest.raises(Exception):
        weather._fetch("synthetic-key", 0, 0, Connection)


def test_daily_details_include_units_and_keep_missing_values_unknown():
    stamp = datetime(2026, 11, 4, 1, tzinfo=timezone.utc).timestamp()
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        def close(self): pass
        status = 200
        def read(self, limit):
            return json.dumps({"timezone": "America/New_York", "flags": {"units": "us"}, "daily": {"data": [
                {"time": stamp, "icon": "rain", "temperatureHigh": 67.1, "temperatureLow": 54.4, "precipProbability": 0.8, "summary": "must not pass through"},
                {"time": stamp + 86400, "icon": "cloudy", "temperatureHigh": True, "temperatureLow": "72", "precipProbability": 0},
                {"time": stamp + 172800, "icon": "wind", "temperatureHigh": 9999, "precipProbability": 1.5},
            ]}}).encode()
    assert weather._fetch("synthetic-key", 0, 0, Connection) == [
        {"date": "2026-11-03", "condition": "rain", "high": 67, "low": 54, "temperature_unit": "F", "precipitation_percent": 80},
        {"date": "2026-11-04", "condition": "cloudy", "precipitation_percent": 0},
        {"date": "2026-11-05", "condition": "wind"},
    ]


def test_wrong_setup_does_not_look_like_a_provider_failure():
    def never(*_):
        pytest.fail("Invalid forecast setup reached the provider")
    for bad in [("fixture", True, None, 0, "synthetic-key"), ("fixture", True, 0, 0, "")]:
        assert weather.forecast(lambda: bad, never)["weather_state"] == "setup_needed"
