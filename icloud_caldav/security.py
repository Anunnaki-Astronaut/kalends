from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class SecurityError(ValueError):
    pass


_ICLOUD_HOST = re.compile(r"^(?:caldav|p[0-9]+-caldav)\.icloud\.com$", re.IGNORECASE)


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int


def validate_caldav_url(raw: str, *, allow_loopback_http: bool = False) -> ValidatedURL:
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SecurityError("invalid CalDAV URL") from exc
    if parsed.username or parsed.password or parsed.fragment:
        raise SecurityError("CalDAV URL must not contain credentials or a fragment")
    if parsed.scheme == "https" and port == 443 and _ICLOUD_HOST.fullmatch(host):
        return ValidatedURL(raw.rstrip("/"), parsed.scheme, host, port)
    if allow_loopback_http and parsed.scheme == "http":
        try:
            if ipaddress.ip_address(host).is_loopback:
                return ValidatedURL(raw.rstrip("/"), parsed.scheme, host, port)
        except ValueError:
            pass
    raise SecurityError("only HTTPS iCloud CalDAV hosts are allowed")


def same_authorized_origin(candidate: str, original: ValidatedURL, *, allow_loopback_http: bool = False) -> ValidatedURL:
    checked = validate_caldav_url(candidate, allow_loopback_http=allow_loopback_http)
    if (checked.scheme, checked.host, checked.port) != (original.scheme, original.host, original.port):
        if not (_ICLOUD_HOST.fullmatch(checked.host) and _ICLOUD_HOST.fullmatch(original.host)):
            raise SecurityError("CalDAV redirect left the authorized iCloud host family")
    return checked


def canonical_url(raw: str, base: str = '') -> str:
    """Reject normalization ambiguities before joining or handing a URL to HTTP."""
    from urllib.parse import quote, unquote, urljoin, urlunsplit
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or any(ord(c) < 33 for c in raw):
        raise SecurityError('invalid resource identity')
    p = urlsplit(raw)
    if p.query or p.fragment or p.username or p.password or '\\' in raw:
        raise SecurityError('ambiguous resource identity')
    path = p.path
    if re.search(r'%(?![0-9a-fA-F]{2})', path) or re.search(r'%(?:2e|2f|5c|25|00|3f|23|3b)', path, re.I):
        raise SecurityError('ambiguous encoded resource identity')
    decoded = unquote(path, errors='strict')
    if ';' in decoded or any(ord(c) < 32 for c in decoded) or '//' in path or any(s in {'.', '..'} for s in decoded.split('/')):
        raise SecurityError('resource path traversal is refused')
    joined = urlsplit(urljoin(base, raw))
    if not joined.scheme or not joined.hostname:
        raise SecurityError('absolute resource identity required')
    host = joined.hostname.lower()
    port = joined.port
    authority = f'[{host}]' if ':' in host else host
    if port and port != (443 if joined.scheme == 'https' else 80):
        authority += f':{port}'
    return urlunsplit((joined.scheme.lower(), authority, quote(unquote(joined.path), safe='/!$&\'()*+,-:;=@_~'), '', ''))


def member_url(collection: str, resource: str) -> str:
    collection = canonical_url(collection)
    resource = canonical_url(resource)
    if not collection.endswith('/') or not resource.startswith(collection) or '/' in resource[len(collection):] or not resource[len(collection):]:
        raise SecurityError('event does not belong to selected calendar')
    return resource
