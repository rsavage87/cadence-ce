"""
Recalls and alerts (slice 19, part C): alert matches, their review, recall work orders, and Check FDA feed, with the Recalls
screen's doors (apps/recalls/permissions.py). Scoped users (apps.workorders.scoping) are refused by every endpoint here.

GET   /api/v1/alert-matches/                   The facility's alert matches, newest notice first. Recalls View.
GET   /api/v1/alert-matches/<id>/              One, with its alert, affected device count, and recall work-order progress. Recalls View.
PATCH /api/v1/alert-matches/<id>/              {"disposition_note": "..."}. Recalls Approve. The status changes only through transition.
POST  /api/v1/alert-matches/<id>/transition/   {"status": "<status>", "note": "..."}: a review move (set_status). Recalls Edit; closing
                                               (closed, not_affected) or reopening a closed one needs Approve.
POST  /api/v1/alert-matches/<id>/work-orders/  One recall work order per affected device without one (create_recall_work_orders).
                                               Recalls Edit.
POST  /api/v1/alert-matches/check-feed/        The screen's Check FDA feed (apps.recalls.feeds.check_feed), no body: fetch the
                                               openFDA recalls posted in the last 30 days, store them, and match them to this facility
                                               only. At most one fetch per feeds.CHECK_COOLDOWN (15 minutes) for everyone, shared with the
                                               screen and the daily import: inside it nothing is fetched ("fetched": false, "again_at"
                                               says when it may be) and what is already stored is still matched here. Returns what it
                                               found: fetched, new_notices, returned, total, truncated (openFDA holds more than it sent),
                                               new_matches (this facility's), checked_at, again_at, last_failed, and message (the screen's
                                               words). Recalls Edit, the screen's level (that of matching alerts to the inventory, which it
                                               extends with the fetch). When openFDA fails: a 502 whose detail is the screen's words;
                                               nothing is matched, and the failure is on the day's record.
"""


from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.models import Level
from apps.recalls import feeds
from apps.recalls import permissions as rc_perms
from apps.recalls import services as rc_services
from apps.recalls.models import AlertMatch
from apps.tenants.context import get_current_tenant
from apps.web.views_recalls import _check_message

from . import serializers as s
from . import serializers_reports as sr
from .base import TenantViewSet


class AlertMatchViewSet(TenantViewSet):
    model, module, serializer_class = AlertMatch, "recalls", s.AlertMatchSerializer
    http_method_names = ["get", "patch", "post", "head", "options"]  # post only for the actions below; create is refused

    @property
    def write_level(self):
        # Same doors as the Recalls screen: the batch and a review move need Edit; editing the note directly stays at Approve.
        if self.action == "work_orders":
            return rc_perms.WORK_ORDERS_LEVEL
        if self.action == "transition":
            return rc_perms.REVIEW_LEVEL
        if self.action == "check_feed":
            return rc_perms.MATCH_LEVEL
        return Level.APPROVE

    def get_queryset(self):
        return AlertMatch.objects.select_related("alert", "device_model").order_by("-alert__published_on", "alert__external_id")

    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed("POST", detail="Matches are created by matching alerts to the inventory, not posted.")

    def perform_update(self, serializer):
        # Status goes through the transition action so it is permission-checked and follows the allowed moves.
        match, d = serializer.instance, serializer.validated_data
        if "status" in d and d["status"] != match.status:
            raise DRFValidationError({"status": "Use POST /api/v1/alert-matches/{id}/transition/ to change the status."})
        serializer.save()

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        match = self.get_object()
        to_status = request.data.get("status")
        if to_status not in AlertMatch.Status.values:
            raise DRFValidationError({"status": f"Required; one of {', '.join(AlertMatch.Status.values)}."})
        if not rc_perms.can_transition(request.user, match.status, to_status):
            raise PermissionDenied("Closing or reopening an alert needs Approve access.")
        try:
            rc_services.set_status(match, to_status, by=request.user, note=request.data.get("note", ""))
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(match).data)

    @action(detail=True, methods=["post"], url_path="work-orders")
    def work_orders(self, request, pk=None):
        match = self.get_object()
        try:
            batch = rc_services.create_recall_work_orders(match, by=request.user)
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"created": batch.created, "unassigned": batch.unassigned, **self.get_serializer(match).data})

    @action(detail=False, methods=["post"], url_path="check-feed")
    def check_feed(self, request):
        if get_current_tenant() is None:  # a superuser who has not picked a tenant: no facility to match
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")
        try:
            check = feeds.check_feed()  # outside any transaction, as the screen calls it: the cooldown's claim commits on its own
        except feeds.FeedError as e:
            detail = f"Could not check the FDA recall feed: {e}. Nothing changed; the daily import will try again."  # the screen's words
            return Response({"detail": detail}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(sr.feed_check(check, _check_message(check)))


def register(router):
    router.register("alert-matches", AlertMatchViewSet, basename="alertmatch")
