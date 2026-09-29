"""
Settings screen (slice 8): integrations, the service request portal, maintenance policy, KPI targets, and risk
scoring, from apps.facility.services. Anyone with Settings View sees the page; changes need Settings Edit
(apps/facility/permissions.py), checked on every POST.
"""
from django.shortcuts import render

from apps.facility import permissions as fac_perms

from .decorators import web_view


@web_view(fac_perms.MODULE, fac_perms.VIEW_LEVEL)
def settings_page(request):
    return render(request, "web/settings.html", {"nav_active": "settings"})
