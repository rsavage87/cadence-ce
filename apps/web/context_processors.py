from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from apps.accounts.models import Level, Module
from apps.credentials.models import Technician
from apps.reports.services import nav_counts
from apps.workorders import scoping

# key, label, icon, url name, module that must be viewable. Order follows the mock's NAV.
NAV = [
    ("overview", "Overview", "dash", "web:overview", Module.REPORTS),
    ("equipment", "Equipment", "eq", "web:equipment", Module.EQUIPMENT),
    ("workorders", "Work orders", "wo", "web:workorders", Module.WORKORDERS),
    ("pm", "PM schedule", "pm", "web:pm", Module.PM),
    ("contracts", "Contracts", "contract", "web:contracts", Module.CONTRACTS),
    ("recalls", "Recalls and alerts", "recall", "web:recalls", Module.RECALLS),
    ("reports", "Reports", "rep", "web:reports", Module.REPORTS),
    ("users", "Users and access", "users", "web:users", Module.USERS),
    ("settings", "Settings", "set", "web:settings", Module.SETTINGS),
]

# Slice 16: the screens a scoped user (apps.workorders.scoping: a vendor's company, a requester's unit) may open, each narrowed to
# their share. Every other screen shows the whole facility, so its views refuse them (web_view's `scoped`) and the nav leaves it out.
SCOPED_SCREENS = {"equipment", "workorders"}


def nav_entries(user) -> list[tuple]:
    """The NAV rows this user may open: View on the module, and for a scoped user only SCOPED_SCREENS."""
    scoped = scoping.is_scoped(user)
    return [row for row in NAV if user.has_level(row[4], Level.VIEW) and (not scoped or row[0] in SCOPED_SCREENS)]


def _shell(request):
    user = request.user
    counts = nav_counts(user)  # a scoped user's badges count their own devices and work orders
    items = [{"key": key, "label": label, "icon": icon, "url": reverse(url_name), "count": counts.get(key), "hot": counts.get(f"{key}_hot", False)}
             for key, label, icon, url_name, _module in nav_entries(user)]
    name = user.get_full_name() or user.username
    initials = "".join(p[0] for p in name.split()[:2]).upper() or "?"
    return {"nav": items, "user_name": name, "initials": initials, "role": user.role.name if user.role_id else ("Superuser" if user.is_superuser else ""),
            # the facility's head count, not for a scoped user (a vendor's or a unit's share has no roster in it)
            "technicians": None if scoping.is_scoped(user) else Technician.objects.filter(is_active=True).count()}


def shell(request):
    """Nav items and counts for the app shell. Lazy, so admin and portal pages that never touch `shell` run no queries."""
    if not getattr(request, "tenant", None) or not request.user.is_authenticated:
        return {}
    return {"shell": SimpleLazyObject(lambda: _shell(request))}
