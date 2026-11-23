"""Additional move integrity/crash cases; run only in run-isolated.sh."""
import sqlite3

import pytest

from test_safe_move import DST, RAW, SRC, move, move_wire, outcome, writes


@pytest.mark.parametrize('before,after', [
    (b'TRIGGER:-PT10M', b'TRIGGER:-PT30M'),
    (b'X-PRIVATE:folded value', b'X-PRIVATE:changed value'),
    (b'X-WR-CALNAME:Keep calendar metadata', b'X-WR-CALNAME:Changed metadata'),
    (b'DTSTART;VALUE=DATE:', b'DTSTART;X-EXTRA=changed;VALUE=DATE:'),
])
def test_move_verifies_fields_outside_display_summary(move_wire, before, after):
    e = move_wire

    def alter(method, path):
        if method == 'PUT':
            etag, data = e.resources[path]
            e.resources[path] = (etag, data.replace(before, after))

    e.state.after = alter
    outcome(move(e), e, {'source_retained', 'indeterminate'})
    assert [row[0] for row in writes(e)] == ['PUT']
    assert e.resources[SRC][1] == RAW


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('boundary', ['before-put', 'after-put'])
def test_crash_recovery_only_reads(move_wire, monkeypatch, boundary):
    e = move_wire
    original = e.svc.client.create

    def crash(*args, **kwargs):
        if boundary.startswith('after'):
            original(*args, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(e.svc.client, 'create', crash)
    with pytest.raises(SimulatedCrash):
        move(e)
    before = list(writes(e))
    result = move(e, e.fresh())
    outcome(result, e, {'source_retained', 'indeterminate'})
    assert writes(e) == before


def test_legacy_reservations_work_after_extension_and_ledger_has_no_content(move_wire):
    e = move_wire
    outcome(move(e), e, {'destination_only'})
    e.svc.operations.reserve('legacy-after-move', e.href)
    with sqlite3.connect(e.svc.operations.path) as db:
        rows = db.execute('SELECT * FROM operations').fetchall()
        assert len(rows) == 2
        stored = repr(rows)
        for content in ('Synthetic', 'original-uid', 'Reminder', 'folded value', 'BEGIN:'):
            assert content not in stored
    assert any(row[1].startswith(DST) for row in writes(e))
