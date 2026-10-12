"""
The Contracts screen's Vendor staff tab (slice 31): the vendors with their attestations on file and their engineers' training, the
engineer drawer, and the forms that add and change them (apps.vendors.services). Contracts View sees it; Contracts Edit changes it.
Scoped users are refused every view here (web_view's default): other vendors' engineers and the facility's evidence are never theirs.

Scaffold: wave A builds the screens behind these names.
"""
from django.http import Http404, HttpResponse

from apps.vendors import permissions as perms

from .decorators import web_view


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_staff(request):
    return HttpResponse("Vendor staff")


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_engineer(request, pk):
    raise Http404
