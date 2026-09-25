from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.accounts.permissions import require_level


def web_view(module=None, level=None):
    """Signed in, working inside a tenant, and (if given) holding `level` on `module`. Checked server-side on every request."""

    def deco(view):
        inner = require_level(module, level)(view) if module else view

        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.tenant is None:
                return render(request, "web/no_tenant.html")
            return inner(request, *args, **kwargs)

        return wrapped

    return deco
