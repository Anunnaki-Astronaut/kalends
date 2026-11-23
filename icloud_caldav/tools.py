from __future__ import annotations

import json
from typing import Any, Callable
from .errors import public_error


def _runtime():
    from .runtime import permission_store, service
    return permission_store, service


def _service():
    return _runtime()[1]()


def _run(fn: Callable[[], Any]) -> str:
    try:
        return json.dumps({"ok": True, "result": fn()}, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps(public_error(exc), ensure_ascii=False)


def _call(method, *args, **kwargs):
    with _service() as svc:
        return getattr(svc, method)(*args, **kwargs)


def _mutate(operation, args):
    denied = {"ok": False, "error": "permission",
              "message": "Calendar writes require fresh user confirmation for this call. No change was made."}
    try:
        args = dict(args)  # Snapshot the final tool arguments; mutation fields are scalars.
        if args.get("explicit_user_request") is not True:
            return json.dumps(denied)
        try:
            from tools.approval_prompt import request_elicitation_consent
        except ImportError:
            return json.dumps(denied)

        from .runtime import prepare_service

        fields = ("operation_id", "calendar_id", "destination_calendar_id", "href", "etag", "title",
                  "start", "end", "description", "location")
        summary = {key: str(args[key])[:200] for key in fields if key in args}
        # Bind the account before asking; do not start the client deadline yet.
        open_service = prepare_service()
        # Unlike a pre_tool_call directive, this does not use standing approvals.
        if request_elicitation_consent(
            f"Calendar {operation}: " + json.dumps(summary, ensure_ascii=True),
            "Review the proposed calendar change. Approve only if you requested it.",
            surface="icloud-caldav", title="Confirm calendar change?",
        ) != "accept":
            return json.dumps(denied)
        with open_service() as svc:
            result = getattr(svc, f"{operation}_event")(**args)
        ok = operation != "move" or result["status"] == "completed"
        return json.dumps({"ok": ok, "result": result}, ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps(public_error(exc), ensure_ascii=False)


def calendars_list(_args: dict, **_kwargs) -> str:
    try:
        with _service() as svc:
            rows = svc.client.discover_calendars()
            for row in rows:
                row["access"] = svc.permissions.access_for(row["id"])
                row["capabilities"] = svc.permissions.capabilities_for(row["id"])
            return json.dumps({"ok": True, "result": rows,
                               "migration_notice": svc.permissions.migration_notice()}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps(public_error(exc), ensure_ascii=False)


def events_query(args: dict, **_kwargs) -> str:
    return _run(lambda: _call("query_events", **args))


def event_get(args: dict, **_kwargs) -> str:
    return _run(lambda: _call("get_event", **args))


def event_create(args: dict, **_kwargs) -> str:
    return _mutate("create", args)


def event_update(args: dict, **_kwargs) -> str:
    return _mutate("update", args)


def event_delete(args: dict, **_kwargs) -> str:
    return _mutate("delete", args)


def event_move(args: dict, **_kwargs) -> str:
    return _mutate("move", args)


HANDLERS = {
    "icloud_calendars_list": calendars_list,
    "icloud_events_query": events_query,
    "icloud_event_get": event_get,
    "icloud_event_create": event_create,
    "icloud_event_update": event_update,
    "icloud_event_delete": event_delete,
    "icloud_event_move": event_move,
}
