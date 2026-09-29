"""
Request-scoped tenant context.

The current tenant lives in a contextvar. TenantManager reads it to scope every query,
TenantModel.save() reads it to stamp new rows, and the middleware sets it per request.
Management commands and tests use `tenant_context()`.
"""
from contextlib import contextmanager
from contextvars import ContextVar

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


@contextmanager
def tenant_context(tenant):
    """Work inside `tenant`: the ORM scope (contextvar) and, on Postgres, the RLS session setting. Both are restored on exit,
    so the public portal, management commands, and nested contexts see the same rows the ORM scope promises."""
    previous = get_current_tenant()
    token = set_current_tenant(tenant)
    set_db_tenant(tenant)
    try:
        yield tenant
    finally:
        reset_current_tenant(token)
        try:
            set_db_tenant(previous)
        except Exception:  # a failed transaction or a closed connection must not hide the original error
            pass
