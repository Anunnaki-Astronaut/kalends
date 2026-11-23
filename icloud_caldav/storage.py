from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .security import SecurityError

CAPABILITIES = ("read", "create", "edit", "delete")
_MIGRATION_NOTICE = (
    "Legacy permissions are in use. Off and Read remain unchanged; Read and write "
    "now allows only Read and Create. Edit and Delete are disabled. Review the "
    "independent controls and select Save calendar access to store version 2 permissions."
)


def validate_permissions(permissions) -> dict[str, dict[str, bool]]:
    """Validate explicit saves without coercion or accepting legacy strings."""
    if not isinstance(permissions, dict):
        raise ValueError("calendar permissions must be capability objects")
    result = {}
    for calendar, caps in permissions.items():
        if (not isinstance(calendar, str) or not calendar
                or not isinstance(caps, dict) or set(caps) != set(CAPABILITIES)
                or any(type(value) is not bool for value in caps.values())
                or (not caps["read"] and any(caps[key] for key in CAPABILITIES[1:]))):
            raise ValueError("each calendar requires read, create, edit, delete booleans; writes require read")
        result[calendar] = dict(caps)
    return result


class PermissionStore:
    """Atomic owner-only store for non-secret per-calendar access policy."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> tuple[dict[str, dict[str, bool]], bool]:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return {}, False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise SecurityError("permission store must be a regular file, not a link")
        if st.st_mode & 0o077:
            raise SecurityError("permission store permissions are broader than owner-only")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid document")
            if "version" in data or "permissions" in data:
                if (set(data) != {"version", "permissions"}
                        or type(data["version"]) is not int or data["version"] != 2):
                    raise ValueError("invalid versioned document")
                return validate_permissions(data["permissions"]), False
            effective = {}
            for calendar, access in data.items():
                if not calendar or not isinstance(access, str) or access not in {"off", "read", "read-write"}:
                    raise ValueError("invalid legacy document")
                effective[calendar] = {
                    "read": access != "off", "create": access == "read-write",
                    "edit": False, "delete": False,
                }
            return effective, True
        except (ValueError, UnicodeError) as exc:
            raise SecurityError("permission store is malformed") from exc

    def capabilities_for(self, calendar_id: str) -> dict[str, bool]:
        return self.all().get(calendar_id, dict.fromkeys(CAPABILITIES, False))

    def access_for(self, calendar_id: str) -> str:
        """Display summary only; never use this value for authorization."""
        caps = self.capabilities_for(calendar_id)
        return "read-write" if any(caps[key] for key in CAPABILITIES[1:]) else "read" if caps["read"] else "off"

    def all(self) -> dict[str, dict[str, bool]]:
        return self._read()[0]

    def migration_notice(self) -> str | None:
        return _MIGRATION_NOTICE if self._read()[1] else None

    def replace(self, permissions: dict[str, dict[str, bool]]) -> None:
        permissions = validate_permissions(permissions)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            existing = os.lstat(self.path)
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise SecurityError("refusing to replace linked/non-regular permission store")
        except FileNotFoundError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=".permissions-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 2, "permissions": permissions}, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600, follow_symlinks=False)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
