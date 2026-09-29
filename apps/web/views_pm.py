"""PM schedule screen (slice 9). Stub until the build agents land."""
from django.shortcuts import render

from apps.pm import permissions as pm_perms

from .decorators import web_view


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def pm_schedule(request):
    return render(request, "web/pm.html", {"nav_active": "pm"})
