"""
Request-scoped tenant context.

The current tenant lives in a contextvar. TenantManager reads it to scope every query,
TenantModel.save() reads it to stamp new rows, and the middleware sets it per request.
Management commands and tests use `tenant_context()`.

Slice 21: working inside a facility also means working in its time zone (Tenant.timezone): tenant_context(), the tenant
middleware, and the API (apps.api.tenancy) activate it, so `timezone.localdate()` is the facility's today and templates show its
local times. Code asks for today with `timezone.localdate()`, never `date.today()` (the server's day).
"""
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

_current_tenant: ContextVar = ContextVar("cadence_current_tenant", default=None)


def get_current_tenant():
    return _current_tenant.get()


def set_current_tenant(tenant):
    """Returns a token to pass to reset_current_tenant()."""
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


def set_db_tenant(tenant):
    """Point the Postgres session setting `app.tenant_id`, which the row-level security policies read, at `tenant`
    (None resets it). A no-op on other databases (the tests run on SQLite)."""
    from django.db import connection

    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        if tenant is None:
            cur.execute("RESET app.tenant_id")
        else:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant.id)])


def zone_of(tenant):
    """`tenant`'s time zone (Tenant.timezone), or the server's (settings.TIME_ZONE) for no facility or a name that is not a zone."""
    for name in (getattr(tenant, "timezone", "") or "", settings.TIME_ZONE):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return ZoneInfo("UTC")


def zone_override(tenant):
    """A context manager that works in `tenant`'s time zone until it exits (the server's for no tenant), restoring what was active."""
    return timezone.override(zone_of(tenant) if tenant is not None else None)


@contextmanager
def tenant_context(tenant):
    """Work inside `tenant`: the ORM scope (contextvar), on Postgres the RLS session setting, and its time zone. All are restored
    on exit, so the public portal, management commands, and nested contexts see the same rows the ORM scope promises."""
    previous = get_current_tenant()
    token = set_current_tenant(tenant)
    zone = zone_override(tenant)
    zone.__enter__()
    set_db_tenant(tenant)
    try:
        yield tenant
    finally:
        zone.__exit__(None, None, None)
        reset_current_tenant(token)
        try:
            set_db_tenant(previous)
        except Exception:  # a failed transaction or a closed connection must not hide the original error
            pass
