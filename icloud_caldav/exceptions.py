"""Shared failure types, independent of transport, service and public rendering."""


class CalDAVError(RuntimeError):
    pass


class AuthenticationError(CalDAVError):
    pass


class PermissionDenied(CalDAVError):
    pass


class ConnectivityError(CalDAVError):
    pass


class ResponseFormatError(CalDAVError):
    pass


class WorkBudgetError(CalDAVError, ValueError):
    """Retain client CalDAVError and service/ICS ValueError compatibility."""
