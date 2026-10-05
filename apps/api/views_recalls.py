"""
Recalls and alerts (slice 19, part C): alert matches, their review, recall work orders, and Check FDA feed.
"""


from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.models import Level
from apps.recalls import permissions as rc_perms
from apps.recalls import services as rc_services
from apps.recalls.models import AlertMatch

from . import serializers as s
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


def register(router):
    router.register("alert-matches", AlertMatchViewSet, basename="alertmatch")
