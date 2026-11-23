from __future__ import annotations

from datetime import date, datetime, timezone
import re
import unicodedata
from types import FunctionType, MethodType
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event

from .exceptions import WorkBudgetError


def _parse_temporal(value: str):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date/time value is required")
    value = value.strip()
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timed events require a timezone-aware ISO 8601 value")
    return parsed.astimezone(timezone.utc)


def build_event(*, title: str, start: str, end: str, uid: str | None = None, description: str | None = None, location: str | None = None) -> bytes:
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500:
        raise ValueError("title is required and must be at most 500 characters")
    start_value, end_value = _parse_temporal(start), _parse_temporal(end)
    if isinstance(start_value, date) != isinstance(end_value, date) or isinstance(start_value, datetime) != isinstance(end_value, datetime):
        raise ValueError("start and end must both be all-day dates or timed values")
    if end_value <= start_value:
        raise ValueError("event end must be after start")
    if any(v is not None and (not isinstance(v,str) or len(v)>16000) for v in (description,location)):
        raise ValueError("text field budget exceeded")
    cal = Calendar()
    cal.add("prodid", "-//Hermes iCloud CalDAV//EN")
    cal.add("version", "2.0")
    event = Event()
    event.add("uid", uid or str(uuid4()))
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", title.strip())
    event.add("dtstart", start_value)
    event.add("dtend", end_value)
    if description is not None:
        event.add("description", description)
    if location is not None:
        event.add("location", location)
    cal.add_component(event)
    return cal.to_ical()


def _single_event(raw: bytes) -> tuple[Calendar, Event]:
    if len(raw) > 2_000_000:
        raise WorkBudgetError("calendar resource is too large")
    cal = Calendar.from_ical(raw)
    events = [component for component in cal.walk() if component.name == "VEVENT"]
    if len(events) != 1:
        raise ValueError("resource must contain exactly one VEVENT")
    return cal, events[0]


def update_event(raw: bytes, **changes: Any) -> bytes:
    cal, event = mutation_eligible(raw)
    allowed = {"title", "start", "end", "description", "location"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported event fields: {sorted(unknown)}")
    mapping = {"title": "SUMMARY", "start": "DTSTART", "end": "DTEND", "description": "DESCRIPTION", "location": "LOCATION"}
    edited_times = {}
    for key, value in changes.items():
        prop = mapping[key]
        if key in {"description","location"} and value is not None and (not isinstance(value,str) or len(value)>16000):
            raise ValueError("text field budget exceeded")
        if key in {"start", "end"}:
            value = _parse_temporal(value)
            original = event[prop]
            if isinstance(value, datetime) != isinstance(original.dt, datetime):
                raise ValueError("time edits must preserve date or datetime kind")
            if "TZID" in original.params:
                try:
                    zone = ZoneInfo(str(original.params["TZID"]))
                except (ZoneInfoNotFoundError, ValueError) as exc:
                    raise ValueError("time edits require a supported IANA timezone") from exc
                if not isinstance(value, datetime):
                    raise ValueError("TZID on all-day dates is unsupported")
                value = value.astimezone(zone)
            # Change only the value: retain every original property parameter.
            original.dt = value
            edited_times[prop] = value
            continue
        if value is None:
            if prop in event:
                del event[prop]
        else:
            if prop in event:
                del event[prop]
            event.add(prop, value)
    if not event.get("SUMMARY") or len(str(event["SUMMARY"]))>500:
        raise ValueError("title required, at most 500 characters")
    _bounds(event)
    event["DTSTAMP"] = datetime.now(timezone.utc)
    serialized = cal.to_ical()
    if edited_times:
        _, reparsed = _single_event(serialized)
        _bounds(reparsed)
        for prop, expected in edited_times.items():
            actual = reparsed.decoded(prop)
            # TZID wall times carry no fold bit. Refuse any instant lost by
            # serialization (including the second fall-back occurrence).
            if (isinstance(actual, datetime) != isinstance(expected, datetime)
                    or _aware(actual).astimezone(timezone.utc)
                    != _aware(expected).astimezone(timezone.utc)):
                raise ValueError("edited time cannot roundtrip its instant")
    return serialized


def event_to_dict(raw: bytes) -> dict[str, Any]:
    if len(raw)>2_000_000: raise WorkBudgetError("resource budget exceeded")
    events=Calendar.from_ical(raw).walk("VEVENT")
    masters=[e for e in events if e.get("RECURRENCE-ID") is None]
    if len(events)>100:
        raise WorkBudgetError("calendar component budget exceeded")
    if not events or len(masters)>1 or len({str(e.get("UID")) for e in events})!=1:
        raise ValueError("unsupported recurrence resource")
    event=masters[0] if masters else events[0]
    result=_event_component_to_dict(event)
    result["recurring"]=len(events)>1 or result["recurring"]
    if result["recurring"]:
        result["exceptions"]=[_event_component_to_dict(e) for e in events if e.get("RECURRENCE-ID") is not None]
    return result


def _format_temporal(value: date) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _event_component_to_dict(event) -> dict[str, Any]:
    def value(name: str):
        prop = event.get(name)
        if prop is None:
            return None
        decoded = event.decoded(name)
        if isinstance(decoded, date):
            return _format_temporal(decoded)
        if isinstance(decoded, bytes):
            return decoded.decode("utf-8", "replace")
        return str(decoded)
    return {
        "uid": value("UID"), "title": value("SUMMARY"), "start": value("DTSTART"),
        "end": value("DTEND"), "description": value("DESCRIPTION"), "location": value("LOCATION"),
        "recurrence_id": value("RECURRENCE-ID"), "status": value("STATUS"),
        "recurring": any(event.get(k) is not None for k in RECURRENCE),
    }


RECURRENCE = ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")
MAX_WORK = 10000
MAX_INSTANCES = 1000


class RecurrenceWorkBudget:
    """Cumulative dateutil period/day work charged across one query."""

    def __init__(self, limit: int = MAX_WORK):
        self.limit = limit
        self.used = 0

    def consume(self, amount: int) -> None:
        if amount < 0 or self.used + amount > self.limit:
            raise WorkBudgetError("recurrence work budget exceeded")
        self.used += amount


class _RecurrenceHorizonReached(Exception):
    pass

_RULE_KEYS = {
    "FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST", "BYSETPOS",
    "BYMONTH", "BYWEEKNO", "BYYEARDAY", "BYMONTHDAY", "BYDAY",
    "BYHOUR", "BYMINUTE", "BYSECOND",
}
_NUMERIC_RULE_DOMAINS = {
    "BYSECOND": (0, 59, False),
    "BYMINUTE": (0, 59, False),
    "BYHOUR": (0, 23, False),
    "BYMONTH": (1, 12, False),
    "BYWEEKNO": (-53, 53, True),
    "BYYEARDAY": (-366, 366, True),
    "BYMONTHDAY": (-31, 31, True),
    "BYSETPOS": (-366, 366, True),
}
_WEEKDAY = re.compile(r"^(?:[+-]?(?:[1-9]|[1-4][0-9]|5[0-3]))?(?:MO|TU|WE|TH|FR|SA|SU)$")


def _rule_values(rule, key):
    values = rule.get(key, [])
    if not isinstance(values, list):
        values = [values]
    if len(values) > 366:
        raise ValueError("recurrence rule is unsupported")
    return values


def _one_rule_value(rule, key, default=None):
    values = _rule_values(rule, key)
    if not values:
        return default
    if len(values) != 1:
        raise ValueError("recurrence rule is unsupported")
    return values[0]


def _rule_integer(value):
    if isinstance(value, bool):
        raise ValueError("recurrence rule is unsupported")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("recurrence rule is unsupported") from exc
    if str(value).strip() != str(parsed):
        raise ValueError("recurrence rule is unsupported")
    return parsed


def _validate_recurrence_rule(rule, first, end):
    if not set(rule).issubset(_RULE_KEYS):
        raise ValueError("recurrence rule is unsupported")
    frequency = str(_one_rule_value(rule, "FREQ", "")).upper()
    if frequency not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ValueError("high frequency recurrence is unsupported")
    interval = _rule_integer(_one_rule_value(rule, "INTERVAL", 1))
    if not 1 <= interval <= MAX_WORK:
        raise ValueError("recurrence rule is unsupported")
    count = _one_rule_value(rule, "COUNT")
    if count is not None:
        count = _rule_integer(count)
        if not 1 <= count <= MAX_WORK:
            raise ValueError("recurrence rule is unsupported")
    until = _one_rule_value(rule, "UNTIL")
    if count is not None and until is not None:
        raise ValueError("recurrence rule is unsupported")
    if until is not None:
        if not isinstance(until, (date, datetime)):
            raise ValueError("recurrence rule is unsupported")
        if isinstance(first, datetime) != isinstance(until, datetime):
            raise ValueError("recurrence rule is unsupported")
        if isinstance(until, datetime) and (until.tzinfo is None or until.utcoffset() is None):
            raise ValueError("recurrence rule is unsupported")
    wkst = _one_rule_value(rule, "WKST")
    if wkst is not None and str(wkst).upper() not in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}:
        raise ValueError("recurrence rule is unsupported")
    weekdays = [str(value).upper() for value in _rule_values(rule, "BYDAY")]
    for weekday in weekdays:
        if not _WEEKDAY.fullmatch(weekday):
            raise ValueError("recurrence rule is unsupported")
    for key, (minimum, maximum, nonzero) in _NUMERIC_RULE_DOMAINS.items():
        for value in _rule_values(rule, key):
            parsed = _rule_integer(value)
            if parsed < minimum or parsed > maximum or (nonzero and parsed == 0):
                raise ValueError("recurrence rule is unsupported")
    if frequency == "WEEKLY" and _rule_values(rule, "BYMONTHDAY"):
        raise ValueError("recurrence rule is unsupported")
    if frequency != "YEARLY" and (_rule_values(rule, "BYYEARDAY") or _rule_values(rule, "BYWEEKNO")):
        raise ValueError("recurrence rule is unsupported")
    if frequency not in {"MONTHLY", "YEARLY"} and any(re.match(r"^[+-]?\d", day) for day in weekdays):
        raise ValueError("recurrence rule is unsupported")
    if frequency == "YEARLY" and _rule_values(rule, "BYWEEKNO") and any(re.match(r"^[+-]?\d", day) for day in weekdays):
        raise ValueError("recurrence rule is unsupported")
    if _rule_values(rule, "BYSETPOS") and not any(
        _rule_values(rule, key) for key in _RULE_KEYS if key.startswith("BY") and key != "BYSETPOS"
    ):
        raise ValueError("recurrence rule is unsupported")

    combinations = 1
    for key in ("BYSECOND", "BYMINUTE", "BYHOUR"):
        combinations *= max(1, len(_rule_values(rule, key)))
    if combinations > 24:
        raise WorkBudgetError("recurrence work budget exceeded")
    first_point = _aware(first)
    if first_point >= end:
        periods = 1
    elif frequency == "DAILY":
        periods = (end - first_point).days // interval + 2
    elif frequency == "WEEKLY":
        periods = (end - first_point).days // (7 * interval) + 2
    else:
        local_end = end.astimezone(first_point.tzinfo)
        if frequency == "MONTHLY":
            distance = (local_end.year - first_point.year) * 12 + local_end.month - first_point.month
        else:
            distance = local_end.year - first_point.year
        periods = max(0, distance) // interval + 2
    period_days = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}[frequency]
    set_positions = max(1, len(_rule_values(rule, "BYSETPOS")))
    if periods * (period_days + set_positions) * combinations > MAX_WORK:
        raise WorkBudgetError("recurrence work budget exceeded")
    return count, until, periods, combinations, set_positions


def _bounded_rule_text(rule, first, end, until):
    parts = rule.to_ical().decode("ascii").split(";")
    retained = [part for part in parts if part.split("=", 1)[0].upper() not in {"COUNT", "UNTIL"}]
    if until is not None and _aware(until) < end:
        retained.append(next(part for part in parts if part.upper().startswith("UNTIL=")))
    elif isinstance(first, datetime):
        retained.append("UNTIL=" + end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    else:
        retained.append("UNTIL=" + end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    return ";".join(retained)


def mutation_eligible(raw):
    if len(raw)>2_000_000: raise WorkBudgetError("resource budget exceeded")
    cal = Calendar.from_ical(raw)
    events = cal.walk("VEVENT")
    if any(any(c.get(k) is not None for k in ("ATTENDEE", "ORGANIZER", "METHOD")) for c in cal.walk()):
        raise ValueError("invitation or scheduling-bearing event mutation is refused")
    if len(events) != 1 or any(any(e.get(k) is not None for k in RECURRENCE) for e in events):
        raise ValueError("recurring event mutation is unsupported")
    event = events[0]
    _bounds(event)
    if event.get("DURATION") is not None:
        raise ValueError("DURATION mutation is unsupported")
    return cal, event


def _aware(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("floating times are unsupported")
        return value
    return datetime.combine(value, datetime.min.time(), timezone.utc)


def _validate_interval(first, last):
    if (isinstance(first, datetime) != isinstance(last, datetime)
            or _aware(last).astimezone(timezone.utc) <= _aware(first).astimezone(timezone.utc)):
        raise ValueError("invalid event interval")


def _bounds(event):
    if event.get("DTSTART") is None or event.get("DTEND") is None:
        raise ValueError("DTSTART and DTEND are required; DURATION-only events are unsupported")
    first, last = event.decoded("DTSTART"), event.decoded("DTEND")
    _validate_interval(first, last)
    return first, last


class EqualTimedBounds(ValueError):
    """One rejected standalone appointment; the exception message is generic."""

    def __init__(self, first: datetime, title: str):
        super().__init__("invalid event interval")
        self.start = first.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        self.title = title


def _standalone_equal_bounds(cal, event, start, end):
    if (any(key in event for key in (*RECURRENCE, "EXRULE", "RELATED-TO", "DURATION"))
            or str(event.get("STATUS", "")).upper() == "CANCELLED"):
        return
    if not all(key in event and not isinstance(event[key], list) for key in ("UID", "DTSTART", "DTEND")):
        return
    if not str(event["UID"]).strip():
        return
    first, last = event.decoded("DTSTART"), event.decoded("DTEND")
    if not all(isinstance(value, datetime) and value.utcoffset() is not None for value in (first, last)):
        return
    instant = first.astimezone(timezone.utc)
    if instant != last.astimezone(timezone.utc) or not start.astimezone(timezone.utc) <= instant < end.astimezone(timezone.utc):
        return
    if (cal.name != "VCALENDAR" or event not in cal.subcomponents
            or any(component.name not in {"VEVENT", "VTIMEZONE"} for component in cal.subcomponents)
            or any(component.errors for component in cal.walk())):
        return
    summary = event.get("SUMMARY", "")
    title = "" if isinstance(summary, list) else "".join(
        " " if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} else ch for ch in str(summary)[:240]
    ).strip()[:120]
    raise EqualTimedBounds(first, title)


def _bounded_rrule_points(iterator, max_periods, combinations, set_positions, work_budget):
    """Iterate dateutil only while its actual period callback stays in range."""
    from dateutil import rrule as dateutil_rrule

    base_iterinfo = dateutil_rrule._iterinfo
    periods = 0

    class BoundedIterInfo(base_iterinfo):
        def rebuild(self, year, month):
            if periods >= max_periods:
                raise _RecurrenceHorizonReached
            return super().rebuild(year, month)

        def _bounded_dayset(self, method, year, month, day):
            nonlocal periods
            if periods >= max_periods:
                raise _RecurrenceHorizonReached
            dayset, first, last = method(year, month, day)
            periods += 1
            work_budget.consume((last - first + set_positions) * combinations)
            return dayset, first, last

        def ddayset(self, year, month, day):
            return self._bounded_dayset(super().ddayset, year, month, day)

        def wdayset(self, year, month, day):
            return self._bounded_dayset(super().wdayset, year, month, day)

        def mdayset(self, year, month, day):
            return self._bounded_dayset(super().mdayset, year, month, day)

        def ydayset(self, year, month, day):
            return self._bounded_dayset(super().ydayset, year, month, day)

    original_iter = getattr(dateutil_rrule.rrule, "_iter")
    bounded_globals = dict(original_iter.__globals__)
    bounded_globals["_iterinfo"] = BoundedIterInfo
    bounded_function = FunctionType(
        original_iter.__code__, bounded_globals, original_iter.__name__,
        original_iter.__defaults__, original_iter.__closure__,
    )
    try:
        yield from MethodType(bounded_function, iterator)()
    except _RecurrenceHorizonReached:
        return


def events_in_range(
    raw: bytes,
    start: datetime,
    end: datetime,
    *,
    work_budget: RecurrenceWorkBudget | None = None,
    classify_equal_bounds: bool = False,
) -> list[dict[str, Any]]:
    """Bound work before expansion, including historical candidates and exceptions."""
    from dateutil.rrule import rrulestr
    if end <= start:
        raise ValueError("range or resource budget exceeded")
    if len(raw) > 2_000_000 or (end-start).days > 366:
        raise WorkBudgetError("range or resource budget exceeded")
    work_budget = work_budget or RecurrenceWorkBudget()
    cal = Calendar.from_ical(raw)
    events = cal.walk("VEVENT")
    if len(events) > 100:
        raise WorkBudgetError("calendar component budget exceeded")
    if not events or len({str(e.get("UID")) for e in events}) != 1:
        raise ValueError("unsupported recurrence resource")
    masters = [e for e in events if e.get("RECURRENCE-ID") is None]
    if len(masters) > 1:
        raise ValueError("multiple recurrence masters")
    exceptions = [e for e in events if e.get("RECURRENCE-ID") is not None]
    for exception in exceptions:
        recurrence_id = exception.get("RECURRENCE-ID")
        if str(recurrence_id.params.get("RANGE", "")).upper() == "THISANDFUTURE":
            raise ValueError("RECURRENCE-ID RANGE=THISANDFUTURE is unsupported")
    overrides = {_aware(e.decoded("RECURRENCE-ID")): e for e in exceptions}
    if len(overrides) != len(events)-len(masters):
        raise ValueError("duplicate detached exception")
    result = []

    def include(first, last):
        if not (_aware(first) < end and _aware(last) > start):
            return False
        if len(result) >= MAX_INSTANCES:
            raise WorkBudgetError("occurrence budget exceeded")
        return True

    def dates(event, key):
        props = event.get(key, [])
        if not isinstance(props, list): props = [props]
        values = [p.dt for prop in props for p in prop.dts]
        if len(values) > MAX_INSTANCES: raise WorkBudgetError("recurrence budget exceeded")
        return {_aware(v) for v in values}
    if masters:
        master = masters[0]
        if classify_equal_bounds and len(events) == 1:
            _standalone_equal_bounds(cal, master, start, end)
        first, last = _bounds(master)
        duration = _aware(last) - _aware(first)
        points = {_aware(first)} | dates(master, "RDATE")
        rule = master.get("RRULE")
        if rule is not None:
            if isinstance(rule, list):
                raise ValueError("multiple RRULE unsupported")
            count, until, periods, combinations, set_positions = _validate_recurrence_rule(rule, first, end)
            rule_start = _aware(first) if isinstance(first, datetime) else datetime.combine(first, datetime.min.time())
            try:
                iterator = rrulestr(_bounded_rule_text(rule, first, end, until), dtstart=rule_start)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("recurrence rule is unsupported") from exc
            bounded_points = _bounded_rrule_points(
                iterator, periods, combinations, set_positions, work_budget,
            )
            for i, point in enumerate(bounded_points):
                if i >= MAX_WORK:
                    raise WorkBudgetError("recurrence work budget exceeded")
                if count is not None and i >= count:
                    break
                point = _aware(point) if isinstance(first, datetime) else point.replace(tzinfo=timezone.utc)
                if point >= end:
                    break
                if point + duration > start:
                    points.add(point)
                if len(points) > MAX_INSTANCES:
                    raise WorkBudgetError("occurrence budget exceeded")
        points -= dates(master, "EXDATE")
        if str(master.get("STATUS", "")).upper() != "CANCELLED":
            base = None
            for point in sorted(points):
                if point in overrides:
                    continue
                finish = point + duration
                occurrence_start, occurrence_end = (point, finish) if isinstance(first, datetime) else (point.date(), finish.date())
                _validate_interval(occurrence_start, occurrence_end)
                if not include(occurrence_start, occurrence_end):
                    continue
                # Share immutable display fields, not copies of the full component.
                if base is None:
                    base = _event_component_to_dict(master)
                result.append(dict(base, start=_format_temporal(occurrence_start), end=_format_temporal(occurrence_end)))
    for event in overrides.values():
        if str(event.get("STATUS", "")).upper() == "CANCELLED": continue
        if include(*_bounds(event)):
            result.append(_event_component_to_dict(event))
    return sorted(result,key=lambda e:e["start"])


def semantically_equal(expected, actual):
    _, left = mutation_eligible(expected)
    _, right = mutation_eligible(actual)
    for key in ("UID", "SUMMARY", "DTSTART", "DTEND", "DESCRIPTION", "LOCATION"):
        a = left.decoded(key) if key in left else None
        b = right.decoded(key) if key in right else None
        if a != b: return False
    return True


def move_digest(raw: bytes) -> str:
    """Digest all calendar properties, parameters and nested components.

    Parsed serialization permits wire folding/line-ending changes, but never
    ignores alarms, timezone definitions, calendar metadata or extension fields.
    Conservative differences are safe refusals, not reasons to discard fields.
    """
    from hashlib import sha256
    cal, event = mutation_eligible(raw)
    if cal.name != 'VCALENDAR' or not event.get('UID'):
        raise ValueError('move requires a calendar and an event UID')
    if any(component.errors for component in cal.walk()):
        raise ValueError('malformed calendar cannot be verified for move')
    return sha256(cal.to_ical()).hexdigest()
