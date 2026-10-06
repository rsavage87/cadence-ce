"""
Moving between a person's facilities (slice 22, apps.accounts.people): the top bar's facility menu and the account menu post here,
and an invitation for someone who already signs in elsewhere links to the join page.

The switch signs the person in to their account in the other facility (django.contrib.auth.login: a new session and CSRF token,
and the account's first sign-in recorded, so the next sign-in lands there). A pending invitation is joined first, with the password
they are signed in with. Everything is checked, and the target's screens read inside its own facility, before login(); the
response is only a redirect, so nothing in this request reads one facility's rows as the other facility's account.

A browser works in one facility at a time: other tabs still showing the old facility reload on their next HTMX request
(apps.web.htmx.FacilityTabMiddleware).
"""
from django.contrib.auth import login
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts import people
from apps.accounts.models import Role, User

from .context_processors import nav_entries
from .decorators import web_view

BACKEND = "apps.accounts.backends.UsernameOrEmailBackend"


def _safe_next(request, url: str) -> str:
    """A path on this site, or ""."""
    if url and url.startswith("/") and not url.startswith("//") and url_has_allowed_host_and_scheme(
            url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return ""


def landing_url(target, screen: str = "") -> str:
    """Where a switch to `target` opens: the screen the person was on (its list, never a record) when `target` may open it in
    its facility, else the Overview (which sends someone without it to their first screen)."""
    with people.in_facility(target) as fresh:
        for key, _label, _icon, url_name, _module in nav_entries(fresh):
            if key == screen:
                return reverse(url_name)
    return reverse("web:overview")


def _go(request, url: str):
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@require_POST
@web_view(scoped=True)  # changes nothing of this facility's: a vendor here may be a director there
def facility_switch(request):
    chosen = request.POST.get("account", "")
    # The facility menu also offers the one the browser is in (on All facilities, the way back) and, without JavaScript, "All
    # facilities" itself: neither is a switch.
    if chosen == "all":
        return _go(request, reverse("web:all_facilities"))
    if chosen == str(request.user.pk):
        return _go(request, landing_url(request.user, request.POST.get("screen", "")))
    target = people.switch_target(request.user, chosen)
    if target is None:
        raise Http404
    if people.is_pending(target) and not people.join(request.user, target):
        raise Http404  # no longer a pending invitation (withdrawn or joined meanwhile)
    url = _safe_next(request, request.POST.get("next", "")) or landing_url(target, request.POST.get("screen", ""))
    fresh = User._default_manager.select_related("tenant").get(pk=target.pk)  # after the join: the password the session carries
    login(request, fresh, backend=BACKEND)
    return _go(request, url)


@web_view(scoped=True)
def facility_join(request, pk):
    """The page an invitation for someone who already signs in elsewhere links to: "Join Lakeside as Technician", a button that
    posts to the switch. Only the person's own pending invitation (or joined account) opens; anything else is a 404."""
    if pk == request.user.pk:
        return redirect("web:overview")
    target = people.switch_target(request.user, pk)
    if target is None:
        raise Http404
    with people.in_facility(target):
        role = Role.objects.filter(pk=target.role_id).values_list("name", flat=True).first() or ""
    return render(request, "web/facility_join.html", {"target": target, "facility": target.tenant.name, "role": role,
                                                       "pending": people.is_pending(target)})
