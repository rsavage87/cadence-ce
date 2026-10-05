"""
Notifications (slice 20, part C): the account menu's Notifications page, where people choose the emails Cadence sends them about
their own work (apps.notifications). Scoped users (apps.workorders.scoping) are not offered it: the emails are about the facility's
work.
"""
from django.http import HttpResponse

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.WORKORDERS, Level.VIEW)
def notifications(request):
    return HttpResponse(status=501)  # built in slice 20, part C
