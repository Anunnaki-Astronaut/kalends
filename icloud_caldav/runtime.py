from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .client import CalDAVClient
from .service import CalendarService
from .storage import PermissionStore

PLUGIN_ID = "icloud-caldav"
SECRET_ENV = "ICLOUD_CALDAV_APP_PASSWORD"
DEFAULT_URL = "https://caldav.icloud.com"


def settings() -> dict[str, str]:
    from hermes_cli.config import load_config_readonly
    raw: Any = load_config_readonly()
    entry = (((raw.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {})
    values = entry.get("settings") or {}
    server = values.get("server_url", DEFAULT_URL)
    if server != DEFAULT_URL:
        raise ValueError("this release supports only https://caldav.icloud.com")
    return {"server_url": server, "apple_id": str(values.get("apple_id") or "").strip()}


def permission_store(cfg=None) -> PermissionStore:
    from plugins.plugin_storage import plugin_data_dir
    account = hashlib.sha256((cfg or settings())["apple_id"].casefold().encode()).hexdigest()
    return PermissionStore(Path(plugin_data_dir(PLUGIN_ID)) / account / "permissions.json")


def prepare_service() -> Callable[[], CalendarService]:
    """Capture the account now, but start the CalDAV deadline only when used."""
    from agent.secret_scope import get_secret_str
    cfg = settings()
    if not cfg["apple_id"]:
        raise ValueError("Apple ID is not configured in Desktop Settings → Plugins → Kalends → Agent settings")
    password = get_secret_str(SECRET_ENV)
    if not password:
        raise ValueError("iCloud app-specific password is not configured")
    store = permission_store(cfg)
    return lambda: CalendarService(CalDAVClient(cfg["server_url"], cfg["apple_id"], password), store)


def service() -> CalendarService:
    return prepare_service()()


# Process incarnation is intentionally part of ownership. Restarted/replaced backends
# require a fresh draft; the UI never needs profile paths or secret-derived data.
import hashlib
import hmac
import secrets
_OWNER_KEY = secrets.token_bytes(32)


def owner_identity():
    from hermes_constants import get_hermes_home
    from agent.secret_scope import get_secret_str
    import json
    try:
        cfg = settings()
    except Exception:
        cfg = {"invalid_configuration": True}
    try:
        secret = get_secret_str(SECRET_ENV)
    except Exception:
        secret = None
    identity = json.dumps([str(get_hermes_home()), cfg, secret], sort_keys=True)
    return hmac.new(_OWNER_KEY, identity.encode(), hashlib.sha256).hexdigest()
