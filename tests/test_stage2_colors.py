"""Synthetic loopback discovery exercises metadata request and XML parsing."""
from xml.sax.saxutils import escape
import pytest
from test_compressed_responses import wire_server


@pytest.mark.parametrize('source,expected', [
    ('#a1B2c3', '#a1b2c3'), ('#A1b2C3FF', '#a1b2c3ff'),
    ('#12345680', '#12345680'),
    ('#12345600', None), ('#12345601', None),
    ('', None), ('red', None), ('#abc', None), ('#abcd', None),
    ('#1234567', None), ('#123456789', None), ('#gg0000', None),
    ('url(https://attacker.invalid/pixel)', None), ('var(--secret)', None),
    ('#123456; background:url(x)', None),
])
def test_discovery_validates_source_color(source, expected):
    xml = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
           'xmlns:a="http://apple.com/ns/ical/">'
           '<d:response><d:href>/synthetic/</d:href><d:propstat><d:prop>'
           '<d:displayname>Synthetic</d:displayname>'
           '<d:resourcetype><c:calendar/></d:resourcetype>'
           '<a:calendar-color>' + escape(source) + '</a:calendar-color>'
           '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
           '</d:response></d:multistatus>').encode()
    with wire_server({'/': (207, {'Content-Type': 'application/xml'}, xml)}) as (client, seen):
        requests = []
        client._client.event_hooks['request'].append(lambda request: requests.append(request.content))
        rows = client.discover_calendars()
        assert len(seen) == 1 and len(rows) == 1
        assert b'calendar-color' in requests[0], 'Discovery must request source color metadata'
        color = rows[0].get('color')
        assert (color.lower() if color else None) == expected


@pytest.mark.parametrize('status,expected', [
    ('HTTP/1.1 200 OK', '#123456'),
    ('HTTP/1.1 404 Not Found', None),
    ('HTTP/1.1 403 Forbidden', None),
    ('', None), ('invalid 200', None),
])
@pytest.mark.parametrize('color_first', [False, True])
def test_color_uses_only_successful_propstats(status, expected, color_first):
    metadata = ('<d:propstat><d:prop><d:displayname>Synthetic</d:displayname>'
                '<d:resourcetype><c:calendar/></d:resourcetype></d:prop>'
                '<d:status>HTTP/1.1 200 OK</d:status></d:propstat>')
    color = ('<d:propstat><d:prop><a:calendar-color>#123456</a:calendar-color>'
             '</d:prop><d:status>' + status + '</d:status></d:propstat>')
    xml = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
           'xmlns:a="http://apple.com/ns/ical/"><d:response><d:href>/synthetic/</d:href>' +
           (color + metadata if color_first else metadata + color) +
           '</d:response></d:multistatus>').encode()
    with wire_server({'/': (207, {'Content-Type': 'application/xml'}, xml)}) as (client, _):
        rows = client.discover_calendars()
        assert len(rows) == 1
        assert rows[0].get('color') == expected
