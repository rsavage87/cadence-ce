"""
The review of each contracted service vendor (slice 34) over the API: read-only (writes are the Contracts screen's Reviews tab's, through
apps.evaluations.vendor_reviews). Seeing a review needs Evaluations View and Contracts View (apps.evaluations.permissions.
can_view_reviews: the screen's door), checked server-side on every request; Module.EVALUATIONS and no scoped_actions: a vendor's or a
requester's account is refused every endpoint (403), whatever its levels, so a vendor's account never reads its own review.

GET vendor-reviews/                  Evaluations View and Contracts View. The latest 12 months first, 50 a page, each without figures or
                                     summary: id, number, vendor (the first name), vendor_names, period_start, period_end, period_label,
                                     status (draft, approved: "Signed off", withdrawn, discarded) and status_label, overall and
                                     overall_label, decision and decision_label, plan_due_on, plan_result and plan_result_label,
                                     plan_closed_on, signed_on, and its page on the web naming the facility (url). ?status= draft |
                                     approved | withdrawn | discarded | all (the default); ?vendor= a vendor's name (the reviews naming
                                     it, any spelling of the same key: "PHILIPS field service" is Philips); ?search= a number;
                                     ?ordering= period_end, period_start, number, signed_on (a leading "-" reverses it).
GET vendor-reviews/<id or number>/   Evaluations View and Contracts View. One review (serializers_vendor_reviews.VendorReviewSerializer):
                                     the list's fields, the ratings ({field, label, covers, rating, rating_label} per criterion),
                                     whether the contract states the expectations, the vendor's reported uptime and response and its
                                     report's reference, the summary, the approver as they signed and who recorded the sign-off, the
                                     previous signed-off review of the vendor (number, overall, decision, plan result), the committees
                                     it was reported to, and the figures through their gate, all or nothing (vendor_figures.cut: also
                                     Reports View and Work orders View; without them the figures are empty and left_out says so). By
                                     number (VR-26-0001, any letter case) or by id.

Another facility's review is a 404, a level missing a 403 in plain words, and any write a 403 (without Edit) or a 405.
"""
import uuid

from django.db.models import Prefetch
from rest_framework import viewsets
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated

from apps.accounts.models import Level, Module
from apps.evaluations import permissions as perms
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import ReviewStatus, VendorReview, VendorReviewReport
from apps.vendors import rules

from . import serializers_vendor_reviews as svr
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

VIEW_REFUSAL = "Reading vendor reviews needs Evaluations View and Contracts View."
WRITE_REFUSAL = ("The API reads vendor reviews only. Drafting, changing, and signing off one is done on the Contracts screen's Reviews "
                 "tab, and needs Evaluations Edit and Contracts View.")
NOT_FOUND = "No vendor review with that id or number in this facility."
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
STATUS_FILTERS = (*ReviewStatus.values, "all")


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


class ContractsView(BasePermission):
    """The Reviews tab's second door: Contracts View, as the screen (permissions.can_view_reviews). Checked after ModulePermission, so a
    scoped user keeps its own refusal."""

    def has_permission(self, request, view):
        if request.user.has_level(Module.CONTRACTS, Level.VIEW):
            return True
        self.message = VIEW_REFUSAL if request.method in SAFE_METHODS else WRITE_REFUSAL
        return False


class VendorReviewViewSet(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """The vendor reviews (the module docstring). Reads need Evaluations View and Contracts View; the figures their own gate (the cut)."""

    permission_classes = [IsAuthenticated, ModulePermission, ContractsView]
    module = perms.MODULE
    delete_level = perms.EDIT_LEVEL  # a DELETE is a write like any other: Edit gets the 405, never a 403 naming a level held
    scoped_actions = frozenset()  # never a scoped user's: a vendor's account never sees its review
    search_fields = ["number"]
    ordering_fields = ["period_end", "period_start", "number", "signed_on"]

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in plain words (a write: Edit, the default write_level, kept so that a write added later is
        closed until its level is set); the scoped refusal and ContractsView's keep their own. With the levels, a write is a 405."""
        if message is None and request.user and request.user.is_authenticated:
            message = VIEW_REFUSAL if request.method in SAFE_METHODS else WRITE_REFUSAL
        super().permission_denied(request, message=message, code=code)

    def get_serializer_class(self):
        return svr.VendorReviewListSerializer if self.action == "list" else svr.VendorReviewSerializer

    def get_queryset(self):
        if self.action != "list":
            reports = VendorReviewReport.objects.select_related("recorded_by").order_by("meeting_on", "created_at")
            return (VendorReview.objects.select_related("snapshot__taken_by", "signed_by", "created_by")
                    .prefetch_related(Prefetch("reports", queryset=reports)))
        qs = VendorReview.objects.all()
        wanted = self.request.query_params.get("status") or "all"
        if wanted not in STATUS_FILTERS:
            raise DRFValidationError({"status": [f"One of {', '.join(STATUS_FILTERS)}."]})
        if wanted != "all":
            qs = qs.filter(status=wanted)
        vendor = self.request.query_params.get("vendor")
        if vendor not in (None, ""):
            key = rules.vendor_key(vendor)
            if not key:
                raise DRFValidationError({"vendor": ["A vendor's name, as its contracts and work orders write it."]})
            # A review names the vendor by one or more keys (a JSON list, which no lookup reads alike on SQLite and PostgreSQL):
            # the facility's reviews' keys read once, in one query, and the ones naming it kept.
            ids = [pk for pk, keys in qs.values_list("pk", "vendor_keys") if key in (keys or ())]
            qs = qs.filter(pk__in=ids)
        return qs.order_by("-period_end", "-number")

    def get_object(self):
        """By id, or by number in any letter case (vendor_reviews.current). Another facility's is a 404, as an unknown one."""
        value = str(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field) or "")
        qs = self.get_queryset()
        pk = _uuid(value)
        if pk is not None:
            found = qs.filter(pk=pk).first()
        else:
            shown = vr.current(value)
            found = qs.filter(pk=shown.pk).first() if shown is not None else None
        if found is None:
            raise NotFound(NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found


def register(router) -> None:
    router.register("vendor-reviews", VendorReviewViewSet, basename="vendor-review")
