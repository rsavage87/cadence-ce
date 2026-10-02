from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from apps.accounts.permissions import require_level
from apps.workorders import scoping


def web_view(module=None, level=None, *, scoped=False):
    """Signed in, working inside a tenant, and (if given) holding `level` on `module`. Checked server-side on every request.

    Slice 16, closed by default: a user whose role sees only a share of the facility (apps.workorders.scoping: a vendor's company,
    a requester's unit) gets a 403 from every view that does not say `scoped=True`, whatever their levels. A view says so only when
    it narrows everything it shows to the user's share (scoping.work_orders / scoping.assets, a 404 for one record out of it), or
    shows nothing of the facility's at all (the password change). The rest (the Overview, PM schedule, Contracts, Recalls, Reports,
    Users, Settings, and every change outside the work orders a scoped user may see) show or change the whole facility."""

    def deco(view):
        inner = require_level(module, level)(view) if module else view

        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.tenant is None:
                return render(request, "web/no_tenant.html")
            if not scoped and scoping.is_scoped(request.user):
                raise PermissionDenied
            return inner(request, *args, **kwargs)

        wrapped.admits_scoped = scoped  # tests/test_scoping_web.py walks the URLs and checks every view either way
        return wrapped

    return deco
