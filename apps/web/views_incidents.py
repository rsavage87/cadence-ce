"""
Incidents (slice 28, beyond the mock): the list and the incident drawer. Wave 2 builds the screen on apps.incidents.services; the
scaffold has the two pages every link needs. Incidents View; scoped users are refused (web_view's default), as every door does.
"""
from django.http import Http404
from django.shortcuts import render

from apps.incidents import permissions as perms
from apps.incidents.models import Incident

from .decorators import web_view


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def incidents(request):
    return render(request, "web/incidents.html", {"incidents": Incident.objects.select_related("asset")[:200]})


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def incident(request, number):
    found = Incident.objects.select_related("asset").filter(number=number).first()
    if found is None:
        raise Http404
    return render(request, "web/incidents.html", {"incidents": [found]})
