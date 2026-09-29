"""
Resolves the tenant for each request and applies it in two places:

1. The Python contextvar that TenantManager uses to scope ORM queries.
2. The Postgres session setting `app.tenant_id` that row-level security policies read
   (see `manage.py enable_rls`). This is the second lock: even a query that forgets the
   ORM scope cannot cross tenants once the app connects as the non-owner role.

Tenant resolution: the signed-in user's tenant; superusers without a tenant can pick one
by storing `tenant_id` in the session (Admin -> Tenants). Public portal views resolve the
tenant from the URL themselves with `tenant_context()`, which sets both as well.
"""
from .context import reset_current_tenant, set_current_tenant, set_db_tenant
from .models import Tenant


def resolve_tenant(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if getattr(user, "tenant_id", None):
        return user.tenant
    if user.is_superuser:
        tenant_id = request.session.get("tenant_id")
        if tenant_id:
            return Tenant.objects.filter(pk=tenant_id, is_active=True).first()
    return None


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = resolve_tenant(request)
        request.tenant = tenant
        token = set_current_tenant(tenant)
        try:
            set_db_tenant(tenant)
            return self.get_response(request)
        finally:
            reset_current_tenant(token)
            try:
                set_db_tenant(None)
            except Exception:  # connection may already be closed
                pass
