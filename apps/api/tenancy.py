"""
The tenant for API requests, whichever way they authenticate.

TenantMiddleware resolves the tenant from the session's user before any view runs. A token request is not authenticated
until DRF does it inside the view, so at middleware time it has no user and no tenant: the ORM scope would return nothing,
saves would fail, and on PostgreSQL row-level security would hide every tenant table. Every API view therefore inherits
TenantAPIMixin (a test walks the URLs to make sure), which sets the tenant right after DRF authenticates: the contextvar and
`app.tenant_id`, before the permission check reads the user's role (a tenant table). Both are restored once the response is
rendered, since the browsable API queries while it renders (form choices, filters).
"""
from apps.tenants.context import get_current_tenant, reset_current_tenant, set_current_tenant, set_db_tenant
from apps.tenants.middleware import resolve_tenant


class TenantAPIMixin:
    _tenant_token = None

    def perform_authentication(self, request):
        super().perform_authentication(request)  # request.user is now the session's user or the token's
        tenant = resolve_tenant(request)
        request._request.tenant = tenant
        self._tenant_token = set_current_tenant(tenant)
        set_db_tenant(tenant)

    def dispatch(self, request, *args, **kwargs):
        previous = get_current_tenant()  # the middleware's: the session user's tenant, or None
        try:
            response = super().dispatch(request, *args, **kwargs)
            if self._tenant_token is not None and callable(getattr(response, "render", None)) and not response.is_rendered:
                response.render()
            return response
        finally:
            if self._tenant_token is not None:
                reset_current_tenant(self._tenant_token)
                try:
                    set_db_tenant(previous)
                except Exception:  # a failed transaction or a closed connection must not hide the original error
                    pass
