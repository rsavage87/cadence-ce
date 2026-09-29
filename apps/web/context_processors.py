from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from apps.accounts.models import Level, Module
from apps.credentials.models import Technician
from apps.reports.services import nav_counts

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


def _shell(request):
    user = request.user
    counts = nav_counts()
    items = []
    for key, label, icon, url_name, module in NAV:
        if user.has_level(module, Level.VIEW):
            items.append({"key": key, "label": label, "icon": icon, "url": reverse(url_name), "count": counts.get(key), "hot": counts.get(f"{key}_hot", False)})
    name = user.get_full_name() or user.username
    initials = "".join(p[0] for p in name.split()[:2]).upper() or "?"
    return {"nav": items, "user_name": name, "initials": initials, "role": user.role.name if user.role_id else ("Superuser" if user.is_superuser else ""),
            "technicians": Technician.objects.filter(is_active=True).count()}


def shell(request):
    """Nav items and counts for the app shell. Lazy, so admin and portal pages that never touch `shell` run no queries."""
    if not getattr(request, "tenant", None) or not request.user.is_authenticated:
        return {}
    return {"shell": SimpleLazyObject(lambda: _shell(request))}
