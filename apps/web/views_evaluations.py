"""
Reports' Program evaluation (slice 32): the facility's annual evaluations of its medical equipment program, a new one for a period, the
evaluation's page (its figures cut to the reader, the text, the objectives, the approvals, its history), and the printable copy for the
committee (apps.evaluations.services and indicators). Program evaluation View reads; Edit drafts and writes; Approve records approvals
and amends. Scoped users are refused every view here (web_view's default).

Scaffold: wave 2A builds the screens behind these names.
"""
from django.http import Http404, HttpResponse

from apps.evaluations import permissions as perms

from .decorators import web_view


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluations(request):
    return HttpResponse("Program evaluation")


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_new(request):
    raise Http404


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluation(request, number, version=None):
    raise Http404


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluation_print(request, number, version=None):
    raise Http404
