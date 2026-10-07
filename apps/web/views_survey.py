"""
The survey binder (slice 25, apps.reports.survey): Reports' "Survey binder" page for a period, a CSV per section table and one of
every gap, and the printable binder. A stub until its build agent fills it in.
"""
from django.http import HttpResponse

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.REPORTS, Level.VIEW)
def survey(request):
    return HttpResponse("Survey binder")


@web_view(Module.REPORTS, Level.VIEW)
def survey_gaps_csv(request):
    return HttpResponse("")


@web_view(Module.REPORTS, Level.VIEW)
def survey_csv(request, section, table):
    return HttpResponse("")


@web_view(Module.REPORTS, Level.VIEW)
def survey_print(request):
    return HttpResponse("Survey binder")
