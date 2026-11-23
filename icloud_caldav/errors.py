"""Only fixed public messages; exception text is never a public API."""
from .client import ConflictError, IndeterminateError, NotFoundError
from .exceptions import (
    AuthenticationError, ConnectivityError, PermissionDenied,
    ResponseFormatError, WorkBudgetError,
)
from .security import SecurityError


def public_error(exc):
    if isinstance(exc, IndeterminateError):
        return {'ok': False, 'error': 'indeterminate', 'message': 'Write outcome requires reconciliation. Read this resource before any further mutation; do not retry with a new operation_id.', 'operation_id': getattr(exc,'operation_id',None), 'href': exc.href, 'calendar_id': exc.href.rsplit('/',1)[0]+'/', 'reconcile': 'icloud_event_get', 'retry_safe': False}
    if isinstance(exc, NotFoundError):
        return {"ok":False,"error":"not_found","message":"Resource absence confirmed by an authenticated 404 read."}
    if isinstance(exc, ConflictError):
        code, message='conflict','Conditional mutation refused. Read current state before proceeding.'
    elif isinstance(exc, SecurityError):
        code,message='security','Resource identity or storage security validation failed.'
    elif isinstance(exc, AuthenticationError):
        code,message='auth','Calendar authentication failed. Check your Apple ID and app-specific password in backend settings.'
    elif isinstance(exc, PermissionDenied):
        code,message='permission',"Calendar access denied. Check this calendar's access policy and account permissions."
    elif isinstance(exc, ConnectivityError):
        code,message='connectivity','Cannot reach the calendar server. Check your network connection and try again.'
    elif isinstance(exc, ResponseFormatError):
        code,message='response_format','The calendar server returned an unreadable response. Try again; if this continues, contact support.'
    elif isinstance(exc, WorkBudgetError):
        code,message='work_budget','Calendar request exceeded safe processing limits. Use a smaller date range or fewer calendars.'
    elif isinstance(exc, ValueError):
        code,message='invalid_request','Invalid input, unsupported event shape, or work budget exceeded.'
    else:
        code,message='request_failed','Calendar request failed. Check configuration and access policy.'
    return {'ok':False,'error':code,'message':message}
