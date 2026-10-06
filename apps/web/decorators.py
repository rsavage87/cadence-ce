from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render

from apps.accounts import people
from apps.accounts.permissions import require_level
from apps.workorders import scoping

FACILITY = "facility"  # the query parameter a staff email's link names its facility by (apps.accounts.people.with_facility)


def web_view(module=None, level=None, *, scoped=False):
    """Signed in, working inside a tenant, and (if given) holding `level` on `module`. Checked server-side on every request.

    Slice 16, closed by default: a user whose role sees only a share of the facility (apps.workorders.scoping: a vendor's company,
    a requester's unit) gets a 403 from every view that does not say `scoped=True`, whatever their levels. A view says so only when
    it narrows everything it shows to the user's share (scoping.work_orders / scoping.assets, a 404 for one record out of it), or
    shows nothing of the facility's at all (the password change). The rest (the Overview, PM schedule, Contracts, Recalls, Reports,
    Users, Settings, and every change outside the work orders a scoped user may see) show or change the whole facility.

    Slice 22, a link for another facility: a full-page GET whose `facility` query names a facility other than the one the browser
    is signed in to (a staff email's link, apps.accounts.people.with_facility) never opens this facility's page: record numbers
    repeat across facilities, so WO-26-0042 here is another work order. It answers before the levels and the scope are checked
    (they are this facility's, and the link is not for it) with `facility_elsewhere`."""

    def deco(view):
        inner = require_level(module, level)(view) if module else view

        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.tenant is None:
                return render(request, "web/no_tenant.html")
            named = linked_facilities(request)
            if named:
                return facility_elsewhere(request, named)
            if not scoped and scoping.is_scoped(request.user):
                raise PermissionDenied
            return inner(request, *args, **kwargs)

        wrapped.admits_scoped = scoped  # tests/test_scoping_web.py walks the URLs and checks every view either way
        return wrapped

    return deco


def linked_facilities(request) -> set:
    """The facility slugs a full-page read's `facility` query names, unless it names only this facility: empty for a link for here
    (no `facility`, an empty one, or this facility's slug). An HTMX request comes from a page already open in this facility (a tab
    left in another reloads first: apps.web.htmx.FacilityTabMiddleware), and a form post from a page this check let through: neither
    is a link, so both are answered here."""
    if request.method not in ("GET", "HEAD") or request.headers.get("HX-Request") == "true":
        return set()
    named = {slug for slug in request.GET.getlist(FACILITY) if slug}
    return set() if named == {request.tenant.slug} else named


def facility_elsewhere(request, named: set):
    """The answer to a link for another facility: "This link is for Lakeside", with a button that switches there (the person's
    joined account, or their pending invitation, which the switch joins) and opens the link (`next`: the full path, query
    included, so it names its facility again and passes this check there). Anyone else, an unknown slug, a facility the person
    cannot open, or a link naming two, gets a 404: never this facility's page, and nothing that says which facilities exist.
    Reads only User and Tenant (apps.accounts.people), so nothing of the other facility's rows."""
    slug = next(iter(named)) if len(named) == 1 else None
    account = next((a for a in people.others(request.user) if a.tenant.slug == slug), None)
    target = people.switch_target(request.user, account.pk) if account is not None else None
    if target is None:
        raise Http404
    return render(request, "web/facility_elsewhere.html", {"target": target, "facility": target.tenant.name, "pending": people.is_pending(target),
                                                           "next": request.get_full_path()})
