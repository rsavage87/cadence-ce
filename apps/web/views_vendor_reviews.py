"""
The Contracts screen's Reviews tab (slice 34): the yearly review of each contracted service vendor (apps.evaluations.vendor_reviews and
vendor_figures): the vendors with their last review and when the next is due, a new review, the review's page (the figures behind their
gate, the ratings, the vendor's reported figures, the decision and its plan, the sign-off, the committees it was reported to), its
print, and the contracted services summary for the governing body. Evaluations View with Contracts View reads; Edit drafts and writes;
Approve signs off. Scoped users are refused every view here (web_view's default): a vendor's account never sees its review.

Scaffold: wave 2A builds the screens behind these names.
"""
from django.http import Http404, HttpResponse

from apps.evaluations import permissions as perms

from .decorators import web_view


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_reviews(request):
    return HttpResponse("Reviews")


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def vendor_review_new(request):
    raise Http404


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_review(request, number):
    raise Http404


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_review_print(request, number):
    raise Http404


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_reviews_print(request):
    raise Http404
