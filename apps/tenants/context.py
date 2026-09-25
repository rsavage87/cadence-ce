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


@contextmanager
def tenant_context(tenant):
    token = set_current_tenant(tenant)
    try:
        yield tenant
    finally:
        reset_current_tenant(token)
