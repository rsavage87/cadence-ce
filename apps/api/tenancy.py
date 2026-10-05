"""
The tenant for API requests, whichever way they authenticate.

TenantMiddleware resolves the tenant from the session's user before any view runs. A token request is not authenticated
until DRF does it inside the view, so at middleware time it has no user and no tenant: the ORM scope would return nothing,
saves would fail, and on PostgreSQL row-level security would hide every tenant table. Every API view therefore inherits
TenantAPIMixin (a test walks the URLs to make sure), which sets the tenant right after DRF authenticates: the contextvar and
`app.tenant_id`, before the permission check reads the user's role (a tenant table). Both are restored once the response is
rendered, since the browsable API queries while it renders (form choices, filters).
"""
from rest_framework.exceptions import ParseError, PermissionDenied

from apps.tenants.context import get_current_tenant, reset_current_tenant, set_current_tenant, set_db_tenant
from apps.tenants.middleware import resolve_tenant

NO_TENANT = "Pick a tenant first (Admin, Tenants)."
BAD_TEXT = "Text cannot contain a null character or an unpaired surrogate."


def bad_text(value) -> bool:
    """Whether a parsed body holds text PostgreSQL cannot store: a NUL, or a lone surrogate (JSON allows "\\ud800"). The request's own
    check (apps.core.http.RejectNulMiddleware) leaves multipart bodies to the form fields that read them, and these views read bodies
    without Django's fields, so the API checks every body once, here."""
    if isinstance(value, str):
        return "\x00" in value or any("\ud800" <= ch <= "\udfff" for ch in value)
    if hasattr(value, "lists"):  # a QueryDict (form or multipart): every value of every key
        return any(bad_text(k) or any(bad_text(v) for v in vs) for k, vs in value.lists())
    if isinstance(value, dict):
        return any(bad_text(k) or bad_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(bad_text(v) for v in value)
    return False


class TenantAPIMixin:
    _tenant_token = None
    needs_facility = True  # every view but the API's root, which lists links only

    def initial(self, request, *args, **kwargs):
        """After DRF authenticates (which sets the tenant) and checks permissions: refuse a user with no facility (a superuser who has
        not picked one; a token can never pick one), so no view reads nobody's rows or tries to save one, and refuse a body holding
        text the database cannot store."""
        super().initial(request, *args, **kwargs)
        if self.needs_facility and get_current_tenant() is None:
            raise PermissionDenied(NO_TENANT)
        if request.method not in ("GET", "HEAD", "OPTIONS") and bad_text(request.data):
            raise ParseError(BAD_TEXT)

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
