"""Named-timezone RED contract; synthetic data only.

Run externally: tests/run-isolated.sh tests/test_named_timezone.py
Authoring does not execute tests: nested bwrap is unavailable in this sandbox.
A TZID wall time has no fold bit. Accept the first fall-back occurrence only
when reparsing preserves its instant; refuse the unrepresentable second one.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event

from icloud_caldav.client import CalDAVClient
from icloud_caldav.exceptions import PermissionDenied
from icloud_caldav.ics import _bounds, mutation_eligible, update_event
from icloud_caldav.service import CalendarService
from icloud_caldav.storage import PermissionStore
from test_caldav_core import CalDAVFixtureHandler, caldav_server


NY = "America/New_York"
ZONES = """BEGIN:VTIMEZONE
TZID:America/New_York
X-SYNTHETIC:keep-zone
BEGIN:STANDARD
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VTIMEZONE
TZID:Custom/Synthetic
X-SYNTHETIC:keep-custom-zone
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0230
TZOFFSETTO:+0230
TZNAME:Synthetic
END:STANDARD
END:VTIMEZONE"""


def resource(start=None, end=None):
    start = start or 'DTSTART;TZID=America/New_York;X-KEEP=start:20260308T013000'
    end = end or 'DTEND;TZID=America/New_York;X-KEEP=end:20260308T043000'
    return (f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic timezone contract//EN
X-CALENDAR-KEEP:calendar-marker
{ZONES}
BEGIN:VEVENT
UID:named-zone-synthetic
DTSTAMP:20260101T000000Z
SUMMARY:Synthetic event
{start}
{end}
X-EVENT-KEEP:event-marker
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Synthetic alarm
TRIGGER:-PT15M
X-ALARM-KEEP:alarm-marker
END:VALARM
END:VEVENT
END:VCALENDAR
""").replace('\n', '\r\n').encode()


def event(raw):
    return Calendar.from_ical(raw).walk('VEVENT')[0]


def temporal_lines(raw):
    # Compare complete unfolded serialized properties, including parameters.
    lines = raw.replace(b'\r\n ', b'').replace(b'\r\n\t', b'').split(b'\r\n')
    return {name: next(line for line in lines
                       if line.split(b':', 1)[0].split(b';', 1)[0] == name)
            for name in (b'DTSTART', b'DTEND')}


def assert_payload_preserved(before, after):
    old, new = Calendar.from_ical(before), Calendar.from_ical(after)
    for name in ('VTIMEZONE', 'VALARM'):
        assert [c.to_ical() for c in new.walk(name)] == [c.to_ical() for c in old.walk(name)]
    assert new['X-CALENDAR-KEEP'] == old['X-CALENDAR-KEEP']
    for name in ('UID', 'X-EVENT-KEEP'):
        assert event(after)[name] == event(before)[name]


def assert_named(raw, prop, zone, wall, instant):
    value = event(raw)[prop]
    assert str(value.params['TZID']) == zone
    assert value.dt.strftime('%Y%m%dT%H%M%S') == wall
    expected = datetime.fromisoformat(instant.replace('Z', '+00:00'))
    assert value.dt.astimezone(timezone.utc) == expected.astimezone(timezone.utc)


@pytest.mark.parametrize('changes', [
    {'title': 'Edited'}, {'description': 'Notes'}, {'location': 'Room'},
    {'description': None, 'location': None},
])
@pytest.mark.parametrize('zone', [NY, 'Custom/Synthetic', 'Unknown/Unresolvable'])
def test_text_only_preserves_serialized_temporal_properties_and_components(zone, changes):
    raw = resource().replace(NY.encode(), zone.encode()) if zone != 'Custom/Synthetic' else resource(
        'DTSTART;TZID=Custom/Synthetic;X-KEEP=start:20260308T013000',
        'DTEND;TZID=Custom/Synthetic;X-KEEP=end:20260308T043000')
    # Unknown zone deliberately has an intact embedded definition: text edits
    # must not need IANA resolution or discard that definition.
    result = update_event(raw, **changes)
    assert temporal_lines(result) == temporal_lines(raw)
    assert_payload_preserved(raw, result)


@pytest.mark.parametrize('start,end,start_wall,end_wall,seconds', [
    ('2026-03-08T06:45:00Z', '2026-03-08T08:15:00+00:00', '20260308T014500', '20260308T041500', 5400),
    ('2026-11-01T00:30:00-04:00', '2026-11-01T02:30:00-05:00', '20261101T003000', '20261101T023000', 10800),
    ('2026-07-01T18:30:00+05:30', '2026-07-01T15:00:00+01:00', '20260701T090000', '20260701T100000', 3600),
])
def test_named_edits_convert_instants_across_dst_and_retain_parameters(start, end, start_wall, end_wall, seconds):
    raw = resource()
    result = update_event(raw, start=start, end=end)
    for prop, wall, instant in [('DTSTART', start_wall, start), ('DTEND', end_wall, end)]:
        assert_named(result, prop, NY, wall, instant)
        assert event(result)[prop].params == event(raw)[prop].params
    first, last = _bounds(event(result))
    assert (last.astimezone(timezone.utc) - first.astimezone(timezone.utc)).total_seconds() == seconds
    assert_payload_preserved(raw, result)


def test_original_zone_is_chosen_independently_for_each_property():
    raw = resource(end='DTEND;TZID=Europe/Berlin;X-KEEP=end:20260308T103000')
    result = update_event(raw, start='2026-03-08T07:30:00Z', end='2026-03-08T09:00:00Z')
    assert_named(result, 'DTSTART', NY, '20260308T033000', '2026-03-08T07:30:00Z')
    assert_named(result, 'DTEND', 'Europe/Berlin', '20260308T100000', '2026-03-08T09:00:00Z')
    for prop in ('DTSTART', 'DTEND'):
        assert event(result)[prop].params == event(raw)[prop].params
    assert_payload_preserved(raw, result)


@pytest.mark.parametrize('named_prop', ['DTSTART', 'DTEND'])
def test_mixed_named_and_utc_properties_keep_their_own_representation(named_prop):
    raw = resource(
        'DTSTART;TZID=America/New_York;X-KEEP=start:20260308T013000'
        if named_prop == 'DTSTART' else 'DTSTART;X-KEEP=start:20260308T063000Z',
        'DTEND;TZID=America/New_York;X-KEEP=end:20260308T043000'
        if named_prop == 'DTEND' else 'DTEND;X-KEEP=end:20260308T083000Z')
    result = update_event(raw, start='2026-03-08T07:00:00Z', end='2026-03-08T08:00:00Z')
    for prop, utc, wall in [('DTSTART', b'20260308T070000Z', '20260308T030000'),
                            ('DTEND', b'20260308T080000Z', '20260308T040000')]:
        assert event(result)[prop].params == event(raw)[prop].params
        if prop == named_prop:
            assert_named(result, prop, NY, wall,
                         '2026-03-08T07:00:00Z' if prop == 'DTSTART' else '2026-03-08T08:00:00Z')
        else:
            assert event(result)[prop].to_ical() == utc


def test_unresolvable_zone_text_edit_retains_conservative_interval_refusal():
    raw = resource(
        'DTSTART;TZID=Unknown/NoDefinition;X-KEEP=start:20260308T013000',
        'DTEND;TZID=Unknown/NoDefinition;X-KEEP=end:20260308T043000')
    with pytest.raises(ValueError, match='floating times are unsupported'):
        update_event(raw, title='Text only')


@pytest.mark.parametrize('start,end', [
    ('DTSTART;X-KEEP=start:20260308T063000Z', 'DTEND;X-KEEP=end:20260308T083000Z'),
    ('DTSTART;VALUE=DATE;X-KEEP=start:20260308', 'DTEND;VALUE=DATE;X-KEEP=end:20260309'),
])
def test_text_edits_preserve_utc_and_all_day_serialized_properties(start, end):
    raw = resource(start, end)
    result = update_event(raw, title='Text only')
    assert temporal_lines(result) == temporal_lines(raw)
    assert_payload_preserved(raw, result)


@pytest.mark.parametrize('field,instant,wall,untouched', [
    ('start', '2026-03-08T07:00:00Z', '20260308T030000', b'DTEND'),
    ('end', '2026-03-08T09:00:00Z', '20260308T050000', b'DTSTART'),
])
def test_one_sided_edit_leaves_other_property_serialization_untouched(field, instant, wall, untouched):
    raw = resource()
    result = update_event(raw, **{field: instant})
    assert temporal_lines(result)[untouched] == temporal_lines(raw)[untouched]
    assert_named(result, 'DT' + field.upper(), NY, wall, instant)
    assert event(result)['DT' + field.upper()].params == event(raw)['DT' + field.upper()].params
    assert_payload_preserved(raw, result)


@pytest.mark.parametrize('field', ['start', 'end'])
@pytest.mark.parametrize('zone', ['Custom/Synthetic', 'Unknown/Unresolvable'])
def test_non_iana_zone_time_edits_are_refused_even_when_embedded_zone_resolves(zone, field):
    raw = resource(
        f'DTSTART;TZID={zone}:20260308T013000',
        f'DTEND;TZID={zone}:20260308T043000')
    with pytest.raises(ValueError):
        update_event(raw, **{field: '2026-03-08T00:00:00Z'})


def test_first_fall_back_occurrence_roundtrips_with_named_zone():
    raw = resource()
    result = update_event(raw, start='2026-11-01T01:30:00-04:00', end='2026-11-01T02:30:00-05:00')
    assert_named(result, 'DTSTART', NY, '20261101T013000', '2026-11-01T05:30:00Z')
    assert_named(result, 'DTEND', NY, '20261101T023000', '2026-11-01T07:30:00Z')


@pytest.mark.parametrize('changes', [
    {'start': '2026-11-01T01:30:00-05:00', 'end': '2026-11-01T02:30:00-05:00'},
    {'start': '2026-11-01T00:30:00-04:00', 'end': '2026-11-01T01:30:00-05:00'},
])
def test_second_fall_back_occurrence_is_refused_instead_of_losing_fold(changes):
    with pytest.raises(ValueError):
        update_event(resource(), **changes)


@pytest.mark.parametrize('start,end,valid', [
    (datetime(2026, 11, 1, 1, 45, fold=0), datetime(2026, 11, 1, 1, 15, fold=1), True),
    (datetime(2026, 11, 1, 1, 15, fold=1), datetime(2026, 11, 1, 1, 45, fold=0), False),
])
def test_bounds_compare_absolute_instants_not_fold_wall_order(start, end, valid):
    # In-memory values isolate _bounds from the separate serialization refusal.
    item = Event()
    zone = ZoneInfo(NY)
    item.add('dtstart', start.replace(tzinfo=zone))
    item.add('dtend', end.replace(tzinfo=zone))
    if valid:
        first, last = _bounds(item)
        assert last.astimezone(timezone.utc) - first.astimezone(timezone.utc) == timedelta(minutes=30)
    else:
        with pytest.raises(ValueError):
            _bounds(item)


@pytest.mark.parametrize('kind', ['utc', 'all-day'])
def test_utc_and_all_day_edits_preserve_value_kind_and_parameters(kind):
    if kind == 'utc':
        raw = resource('DTSTART;X-KEEP=start:20260308T063000Z', 'DTEND;X-KEEP=end:20260308T083000Z')
        changes = {'start': '2026-03-08T01:45:00-05:00', 'end': '2026-03-08T04:15:00-04:00'}
        expected = [b'20260308T064500Z', b'20260308T081500Z']
    else:
        raw = resource('DTSTART;VALUE=DATE;X-KEEP=start:20260308', 'DTEND;VALUE=DATE;X-KEEP=end:20260309')
        changes = {'start': '2026-03-09', 'end': '2026-03-11'}
        expected = [b'20260309', b'20260311']
    result = update_event(raw, **changes)
    for prop, serialized in zip(('DTSTART', 'DTEND'), expected):
        value = event(result)[prop]
        assert value.to_ical() == serialized
        assert value.params == event(raw)[prop].params
        assert 'TZID' not in value.params
        assert type(value.dt) is (datetime if kind == 'utc' else date)
    assert_payload_preserved(raw, result)


@pytest.mark.parametrize('changes', [
    {'start': '2026-03-08'}, {'end': '2026-03-09'},
    {'start': '2026-03-08', 'end': '2026-03-09'},
])
def test_timed_to_all_day_type_changes_are_refused(changes):
    with pytest.raises(ValueError):
        update_event(resource(), **changes)


@pytest.mark.parametrize('changes', [
    {'start': '2026-03-08T00:00:00Z'}, {'end': '2026-03-09T00:00:00Z'},
    {'start': '2026-03-08T00:00:00Z', 'end': '2026-03-09T00:00:00Z'},
])
def test_all_day_to_timed_type_changes_are_refused(changes):
    raw = resource('DTSTART;VALUE=DATE:20260308', 'DTEND;VALUE=DATE:20260309')
    with pytest.raises(ValueError):
        update_event(raw, **changes)


@pytest.mark.parametrize('changes', [
    {'end': '2026-03-08T06:30:00Z'}, {'end': '2026-03-08T06:00:00Z'},
    {'start': '2026-03-08T09:00:00Z'},
])
def test_nonpositive_absolute_interval_is_refused(changes):
    with pytest.raises(ValueError):
        update_event(resource(), **changes)


def test_named_zone_still_passes_existing_mutation_gate():
    _, item = mutation_eligible(resource())
    assert str(item['UID']) == 'named-zone-synthetic'


@pytest.mark.parametrize('caldav_server', ['socket'], indirect=True)
@pytest.mark.parametrize('scenario', ['time', 'text', 'unsafe-fold', 'read-only'])
def test_service_real_wire_update_and_verified_readback(caldav_server, tmp_path, monkeypatch, scenario):
    calendar = caldav_server + '/calendars/work/'
    path = '/calendars/work/named-zone.ics'
    raw = resource()
    CalDAVFixtureHandler.resources[path] = ('"seed"', raw)
    store = PermissionStore(tmp_path / 'permissions.json')
    store.replace({calendar: {'read': True, 'create': False,
                              'edit': scenario != 'read-only', 'delete': False}})
    before_permissions = store.path.read_bytes()
    seen = []
    for method in ('GET', 'PUT'):
        original = getattr(CalDAVFixtureHandler, 'do_' + method)
        def handle(self, _method=method, _original=original):
            seen.append((_method, self.path, self.headers.get('If-Match')))
            return _original(self)
        monkeypatch.setattr(CalDAVFixtureHandler, 'do_' + method, handle)
    client = CalDAVClient(caldav_server, 'apple@example.com', 'app-password', allow_loopback_http=True)
    service = CalendarService(client, store)
    changes = {'title': 'Edited over TCP'} if scenario == 'text' else {
        'start': '2026-03-08T07:00:00Z', 'end': '2026-03-08T08:00:00Z'}
    if scenario == 'unsafe-fold':
        changes = {'start': '2026-11-01T01:30:00-05:00', 'end': '2026-11-01T02:30:00-05:00'}
    try:
        def update():
            return service.update_event(calendar, caldav_server + path, etag='"seed"',
                                        explicit_user_request=True,
                                        operation_id='named-zone-' + scenario, **changes)
        if scenario in ('unsafe-fold', 'read-only'):
            with pytest.raises(PermissionDenied if scenario == 'read-only' else ValueError):
                update()
            assert seen == ([] if scenario == 'read-only' else [('GET', path, None)])
            assert CalDAVFixtureHandler.resources[path] == ('"seed"', raw)
        else:
            reply = update()
            assert seen == [('GET', path, None), ('PUT', path, '"seed"'), ('GET', path, None)]
            etag, stored = CalDAVFixtureHandler.resources[path]
            assert reply['verified'] is True
            assert reply['etag'] == etag != '"seed"'
            assert reply['uid'] == 'named-zone-synthetic'
            assert reply['href'] == caldav_server + path
            readback = service.get_event(calendar, caldav_server + path)
            assert readback['start'] == reply['start']
            assert readback['end'] == reply['end']
            if scenario == 'text':
                assert temporal_lines(stored) == temporal_lines(raw)
                assert readback['title'] == 'Edited over TCP'
            else:
                assert reply['start'] == '2026-03-08T03:00:00-04:00'
                assert reply['end'] == '2026-03-08T04:00:00-04:00'
                assert_named(stored, 'DTSTART', NY, '20260308T030000', changes['start'])
                assert_named(stored, 'DTEND', NY, '20260308T040000', changes['end'])
                for prop in ('DTSTART', 'DTEND'):
                    assert event(stored)[prop].params == event(raw)[prop].params
            assert_payload_preserved(raw, stored)
        assert store.path.read_bytes() == before_permissions
    finally:
        service.close()
