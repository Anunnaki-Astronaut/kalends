from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Callable

from .client import CalDAVClient
from .exceptions import PermissionDenied, WorkBudgetError
from .ics import (
    EqualTimedBounds,
    RecurrenceWorkBudget,
    build_event,
    event_to_dict,
    events_in_range,
    update_event,
    mutation_eligible,
)
from .storage import PermissionStore
from .security import canonical_url, member_url
from .operations import Operations
from .client import ConflictError, IndeterminateError
from uuid import uuid5, NAMESPACE_URL


# Match the 2 MB transport budget, reserving space for the tool/dashboard envelope.
_MAX_QUERY_EVENTS_BYTES = 2_000_000 - 128


class CalendarService:
    def __init__(self, client: CalDAVClient, permissions: PermissionStore):
        self.client = client
        self.permissions = permissions
        self.operations = Operations(permissions.path.with_name("operations.sqlite3"))

    def _require(self, calendar_id: str, capability: str = "read") -> None:
        if canonical_url(calendar_id) != calendar_id or not calendar_id.endswith("/"):
            raise ValueError("canonical absolute collection identity required")
        caps = self.permissions.capabilities_for(calendar_id)
        if not caps["read"] or not caps[capability]:
            raise PermissionDenied(f"calendar capability denied ({capability})")

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("date range values must be timezone-aware")
        return parsed

    @staticmethod
    def _mutation_request(asserted: bool) -> None:
        if asserted is not True:
            raise PermissionDenied("mutation requires an explicit current user request")

    def query_events(
        self, calendar_id: str, start: str, end: str, *,
        phase_observer: Callable[[str | None], None] | None = None,
        skipped: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Only the authenticated Desktop query opts into partial results.
        if phase_observer is not None:
            phase_observer(None)
        self._require(calendar_id)
        if phase_observer is not None:
            phase_observer("request_validation")
        first, last = self._time(start), self._time(end)
        if last <= first:
            raise ValueError("range end must be after start")
        if last - first > timedelta(days=366):
            raise WorkBudgetError("date range may not exceed 366 days")
        if phase_observer is not None:
            phase_observer(None)
        result = []
        encoded_bytes = 2  # JSON array brackets
        rejected = 0
        examples = []
        work_budget = RecurrenceWorkBudget()
        if phase_observer is not None:
            phase_observer("provider_read")
        resources = self.client.query(calendar_id, first, last)
        if phase_observer is not None:
            phase_observer("event_processing")
        for resource in resources:
            member_url(calendar_id, resource.href)
            try:
                entries = events_in_range(resource.data, first, last, work_budget=work_budget,
                                          classify_equal_bounds=skipped is not None)
            except EqualTimedBounds as issue:
                if skipped is None:
                    raise
                rejected += 1
                if len(examples) < 5:
                    examples.append({"code": "equal_start_end", "title": issue.title, "start": issue.start})
                continue
            for event in entries:
                event.update({"href": resource.href, "etag": resource.etag, "calendar_id": calendar_id})
                if len(result) >= 1000:
                    raise WorkBudgetError("query occurrence budget exceeded")
                # The tool's spaced JSON also bounds the dashboard's compact JSON.
                encoded_bytes += len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
                if result:
                    encoded_bytes += 2  # comma and space
                if encoded_bytes > _MAX_QUERY_EVENTS_BYTES:
                    raise WorkBudgetError("query response budget exceeded")
                result.append(event)
        result.sort(key=lambda event: (event.get("start") or "", event.get("uid") or ""))
        # Permissions may change during transport or recurrence expansion.
        if phase_observer is not None:
            phase_observer(None)
        self._require(calendar_id)
        if rejected and skipped is not None:
            diagnostics = dict(count=rejected, examples=examples)
            diagnostic_bytes = len(json.dumps({'skipped': diagnostics}, ensure_ascii=False).encode('utf-8'))
            if encoded_bytes + diagnostic_bytes > _MAX_QUERY_EVENTS_BYTES:
                raise WorkBudgetError("query response budget exceeded")
            skipped.update(diagnostics)
        return result

    def get_event(self, calendar_id: str, href: str) -> dict[str, Any]:
        self._require(calendar_id)
        member_url(calendar_id, href)
        resource = self.client.get(href)
        result = event_to_dict(resource.data)
        result.update({"href": resource.href, "etag": resource.etag, "calendar_id": calendar_id})
        self._require(calendar_id)
        return result

    def create_event(self, calendar_id: str, *, title: str, start: str, end: str, explicit_user_request: bool, operation_id: str, description: str | None = None, location: str | None = None, uid: str | None = None) -> dict[str, Any]:
        self._require(calendar_id, "create")
        self._mutation_request(explicit_user_request)
        self.operations.check(operation_id)
        from .operations import operation_id as validate_operation
        validate_operation(operation_id)
        deterministic_uid = str(uuid5(NAMESPACE_URL, calendar_id + "#" + operation_id))
        if uid is not None and uid != deterministic_uid:
            raise ValueError("UID is derived from operation_id; omit uid")
        uid = deterministic_uid
        data = build_event(title=title, start=start, end=end, uid=uid, description=description, location=location)
        built = event_to_dict(data)
        href = calendar_id + built["uid"] + ".ics"
        resource = self._write(operation_id, href, calendar_id, "create", lambda: self.client.create(calendar_id, built["uid"], data))
        result = event_to_dict(resource.data)
        result.update({"href": resource.href, "etag": resource.etag, "calendar_id": calendar_id, "operation_id": operation_id, "verified": True})
        return result

    def update_event(self, calendar_id: str, href: str, *, etag: str, explicit_user_request: bool, operation_id: str, **changes: Any) -> dict[str, Any]:
        self._require(calendar_id, "edit")
        self._mutation_request(explicit_user_request)
        self.operations.check(operation_id)
        member_url(calendar_id, href)
        current = self.client.get(href)
        if current.etag != etag:
            from .client import ConflictError
            raise ConflictError("event changed before update; no overwrite occurred")
        data = update_event(current.data, **changes)
        resource = self._write(operation_id, href, calendar_id, "edit", lambda: self.client.update(href, etag, data))
        result = event_to_dict(resource.data)
        result.update({"href": resource.href, "etag": resource.etag, "calendar_id": calendar_id, "operation_id": operation_id, "verified": True})
        return result

    def delete_event(self, calendar_id: str, href: str, *, etag: str, explicit_user_request: bool, operation_id: str) -> dict[str, Any]:
        self._require(calendar_id, "delete")
        self._mutation_request(explicit_user_request)
        self.operations.check(operation_id)
        member_url(calendar_id, href)
        current = self.client.get(href)
        if current.etag != etag:
            from .client import ConflictError
            raise ConflictError("event changed before delete; it was not deleted")
        mutation_eligible(current.data)
        self._write(operation_id, href, calendar_id, "delete", lambda: self.client.delete(href, etag))
        return {"deleted": True, "href": href, "calendar_id": calendar_id, "operation_id": operation_id, "verified": True}

    def move_event(self, calendar_id: str, href: str, *, destination_calendar_id: str,
                   etag: str, explicit_user_request: bool, operation_id: str) -> dict[str, Any]:
        """Copy once, verify the whole payload, and retain the source.

        A reservation is never released, including after crashes or conflicts.
        Repeated calls only observe; they cannot resume a destructive transition.
        """
        import hashlib
        import json
        import re
        from .ics import move_digest
        from .operations import operation_id as validate_operation

        self._mutation_request(explicit_user_request)
        validate_operation(operation_id)
        self._move_permissions(calendar_id, destination_calendar_id)
        if calendar_id == destination_calendar_id:
            raise ValueError('move requires different collections')
        if member_url(calendar_id, href) != href:
            raise ValueError('canonical absolute member identity required')
        if not isinstance(etag, str) or not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', etag):
            raise ValueError('move requires one strong exact ETag')
        # Validate both targets against the existing transport's origin policy
        # before reading either resource.
        self.client._resource_url(calendar_id)
        self.client._resource_url(destination_calendar_id)
        fingerprint = hashlib.sha256(json.dumps(
            ['move-v1', calendar_id, href, destination_calendar_id, etag],
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        slug = 'move-' + str(uuid5(NAMESPACE_URL, destination_calendar_id + '#' + operation_id))
        destination = member_url(destination_calendar_id, destination_calendar_id + slug + '.ics')
        previous = self.operations.find_move(operation_id, fingerprint)
        if previous is not None:
            return self._reconcile_move(calendar_id, href, destination_calendar_id,
                                        destination, operation_id, previous)

        current = self.client.get(href)
        if current.href != href or current.etag != etag:
            raise ConflictError('source changed before move; no write occurred')
        digest = move_digest(current.data)
        self._move_permissions(calendar_id, destination_calendar_id)
        previous = self.operations.reserve_move(operation_id, destination, fingerprint, digest)
        if previous is not None:
            return self._reconcile_move(calendar_id, href, destination_calendar_id,
                                        destination, operation_id, previous)
        record = {'move_digest': digest, 'move_phase': 'put_intent'}

        def phase(value):
            self.operations.move_phase(operation_id, fingerprint, record['move_phase'], value)
            record['move_phase'] = value

        # This refusal is still preflight: no PUT has been attempted.
        self._move_permissions(calendar_id, destination_calendar_id)
        try:
            try:
                copied = self.client.create(destination_calendar_id, slug, current.data)
            except ConflictError:
                phase('collision')
            else:
                self._move_permissions(calendar_id, destination_calendar_id)
                if copied.href != destination or move_digest(copied.data) != digest:
                    raise ValueError('destination payload could not be verified')
                phase('copy_verified')
                # A source-only conditional DELETE cannot protect the destination.
                # Without a server-side guarantee for both resources, stop at copy.
        except Exception:
            # Lost replies, denied access and failed readbacks are partial
            # outcomes. Never retry a write or remove the destination copy.
            pass
        return self._reconcile_move(calendar_id, href, destination_calendar_id,
                                    destination, operation_id, record)

    def _move_permissions(self, source, destination):
        self._require(source, 'delete')
        self._require(destination, 'create')

    @staticmethod
    def _move_outcome(status, identity, source, destination):
        result = {'status': status, 'verified': status == 'completed',
                  'operation_id': identity, 'source_href': source,
                  'destination_href': destination, 'retry_safe': False}
        if status != 'completed':
            result['manual_action'] = (
                'Read both resources and review calendar permissions to reconcile this move. '
                'Keep the destination copy; do not retry with a new operation_id. '
                'Any further change requires a separate explicit user decision.')
        return result

    def _reconcile_move(self, source_calendar, source, destination_calendar,
                        destination, identity, record):
        from .client import NotFoundError
        from .ics import move_digest

        def observe(calendar, href):
            self._require(calendar)
            try:
                resource = self.client.get(href)
            except NotFoundError:
                self._require(calendar)
                return None
            self._require(calendar)
            if resource.href != href:
                raise ValueError('unexpected resource identity')
            return resource

        status = 'indeterminate'
        try:
            self._move_permissions(source_calendar, destination_calendar)
            original = observe(source_calendar, source)
            copy = observe(destination_calendar, destination)
            # Only a successful conditional create plus full readback attributes
            # a copy to this operation. A collision can never become success.
            # Delete phases exist only in historical ledger records.
            attributable = record['move_phase'] in {'copy_verified', 'delete_intent', 'completed'}
            matching = False
            if copy is not None and attributable:
                matching = move_digest(copy.data) == record['move_digest']
            if matching and original is not None:
                status = 'destination_only'
            elif matching and record['move_phase'] in {'delete_intent', 'completed'}:
                status = 'completed'
            elif original is not None and (copy is None or record['move_phase'] == 'collision'):
                status = 'source_retained'
            self._move_permissions(source_calendar, destination_calendar)
        except Exception:
            status = 'indeterminate'
        return self._move_outcome(status, identity, source, destination)

    def _write(self, identity, href, calendar_id, capability, action):
        self.operations.reserve(identity, href)
        try:
            # Reload policy after GET/build and durable reservation, just before transport.
            self._require(calendar_id, capability)
        except Exception:
            # No write was attempted; this reservation is safe to release.
            self.operations.release(identity)
            raise
        try:
            return action()
        except ConflictError:
            self.operations.release(identity)
            raise
        except IndeterminateError as error:
            error.operation_id = identity
            raise

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
