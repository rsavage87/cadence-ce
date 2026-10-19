"""
The CE department's test equipment (slice 35) over the API: read only (adding, changing, status moves, calibrations, and tolerance reviews
are the Equipment screen's Test equipment tab's, through apps.testequipment.services). Equipment View (apps.testequipment.permissions.
can_view: the screen's door), checked server-side on every request; Module.EQUIPMENT and no scoped_actions: a vendor's or a requester's
account is refused every endpoint (403), whatever its levels.

GET test-equipment/                Equipment View. Every item by tag, 50 a page (serializers_testequipment.TestEquipmentListSerializer):
                                   id, tag, kind and its label, manufacturer, model, serial, program (calibration or verification: what
                                   its certificates are) and its label, interval_months, status, status_reason, and their labels,
                                   status_since, held_by (a technician's id) and held_by_name, standing (apps.testequipment.rules' words
                                   judged on the facility's today: "Calibration due Mar 31, 2027", "Calibration was due Aug 31, 2026",
                                   "No calibration on file", "Failed calibration Oct 3, 2026"), due_on and last_calibrated_on (the
                                   certificate the standing rests on), and its page on the web naming the facility (url). In a fixed
                                   number of queries (services.list_with_standing: the items and their calibrations, whatever their
                                   number). ?standing= past_due | due_soon (due within 60 days) | no_record | failed (rules.FILTERS);
                                   ?status= a Status value; ?kind= a Kind value; ?held_by= a technician's id; ?search= a tag or serial;
                                   ?ordering= tag, kind, status, status_since, manufacturer (a leading "-" reverses it).
GET test-equipment/<id or tag>/    Equipment View. One item (TestEquipmentSerializer), by id or by tag in any letter case: the list's
                                   fields, its calibrations (newest first, each with its days, provider, certificate number, whether
                                   the certificate carries an ISO/IEC 17025 accreditation mark, as found, adjusted, result, who recorded
                                   it and the day it was entered, and whether it was marked entered in error), and the tolerance reviews
                                   (one per live calibration found out of tolerance or failed: the look-back window and how much work is
                                   in it now, "Not reviewed" or the decision with its frozen count, work added since, recheck progress;
                                   the work orders in the window only for Work orders View too).

Which test equipment a work order recorded is the work order's: its test_equipment field, the transition to completed's test_equipment,
and POST work-orders/{id}/test-equipment/ (apps/api/views.py). Another facility's item is a 404, a level missing a 403 in plain words, and
any write a 405 saying where the change is made (with Equipment Edit) or a 403 (without it).
"""
import uuid

from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import MethodNotAllowed, NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.testequipment import permissions as perms
from apps.testequipment import rules
from apps.testequipment import services as te_services
from apps.testequipment.models import Calibration, Kind, Status, TestEquipment

from . import serializers_testequipment as ste
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

VIEW_REFUSAL = "Reading test equipment needs Equipment View."
WRITE_REFUSAL = ("The API reads test equipment only. Adding and changing test equipment, recording its calibrations, and deciding a "
                 "tolerance review are done on the Equipment screen's Test equipment tab, and need Equipment Edit (a review, Work orders "
                 "Approve).")
READ_ONLY = ("The API reads test equipment only: add and change it, record its calibrations, and decide its tolerance reviews on the "
             "Equipment screen's Test equipment tab.")
NOT_FOUND = "No test equipment with that id or tag in this facility."
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _one_of(values) -> list:
    return [f"One of {', '.join(values)}."]


class TestEquipmentViewSet(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """The test equipment (the module docstring). Reads need Equipment View; nothing is written here."""
    __test__ = False  # pytest: not a test class

    permission_classes = [IsAuthenticated, ModulePermission]
    module = perms.MODULE
    delete_level = perms.EDIT_LEVEL  # a DELETE is a write like any other: Edit gets the 405, never a 403 naming a level held
    scoped_actions = frozenset()  # never a scoped user's
    lookup_value_regex = "[^/]+"  # a tag may hold a dot (TAG_VALIDATOR refuses only spaces and slashes)
    search_fields = ["tag", "serial"]
    ordering_fields = ["tag", "kind", "status", "status_since", "manufacturer"]

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in plain words (a write: Edit, the default write_level, kept so that a write added later is
        closed until its level is set); the scoped refusal keeps its own. With the level, a write is a 405 (http_method_not_allowed)."""
        if message is None and request.user and request.user.is_authenticated:
            message = VIEW_REFUSAL if request.method in SAFE_METHODS else WRITE_REFUSAL
        super().permission_denied(request, message=message, code=code)

    def http_method_not_allowed(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=READ_ONLY)

    def get_serializer_class(self):
        return ste.TestEquipmentListSerializer if self.action == "list" else ste.TestEquipmentSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.setdefault("today", self._today())
        return context

    def _today(self):
        """The facility's today (its time zone is active: TenantAPIMixin), read once per request."""
        if not hasattr(self, "_today_value"):
            self._today_value = timezone.localdate()
        return self._today_value

    def get_queryset(self):
        qs = TestEquipment.objects.select_related("held_by")
        if self.action == "list":
            params = self.request.query_params
            errors = {}
            status = params.get("status") or ""
            if status and status not in Status.values:
                errors["status"] = _one_of(Status.values)
            kind = params.get("kind") or ""
            if kind and kind not in Kind.values:
                errors["kind"] = _one_of(Kind.values)
            held_by = params.get("held_by") or ""
            holder = _uuid(held_by) if held_by else None
            if held_by and holder is None:
                errors["held_by"] = ["A technician's id."]
            standing = params.get("standing") or ""
            if standing and standing not in rules.FILTERS:
                errors["standing"] = _one_of(rules.FILTERS)
            if errors:
                raise DRFValidationError(errors)
            if status:
                qs = qs.filter(status=status)
            if kind:
                qs = qs.filter(kind=kind)
            if holder is not None:
                qs = qs.filter(held_by_id=holder)
        return qs.order_by("tag_key", "pk")

    def list(self, request, *args, **kwargs):
        """Each item with its standing judged today (services.list_with_standing: two queries for any number of items), then the
        standing filter (rules.matches, the list's and the CSV's), then the page."""
        today = self._today()
        items = te_services.list_with_standing(self.filter_queryset(self.get_queryset()), today)
        wanted = request.query_params.get("standing") or ""
        if wanted:
            items = [i for i in items if rules.matches(i.standing, wanted, today)]
        page = self.paginate_queryset(items)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(items, many=True).data)

    def get_object(self):
        """By id, or by tag in any letter case (tag_key). Another facility's is a 404, as an unknown one."""
        value = str(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field) or "")
        qs = self.get_queryset()
        pk = _uuid(value)
        found = qs.filter(pk=pk).first() if pk is not None else None
        if found is None and value:
            found = qs.filter(tag_key=value.casefold()).first()
        if found is None:
            raise NotFound(NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found

    def retrieve(self, request, *args, **kwargs):
        """One item: its calibrations read once, for the calibrations, the reviews, and its standing today."""
        item = self.get_object()
        cals = list(Calibration.objects.filter(test_equipment=item).select_related("recorded_by", "entered_in_error_by"))
        item.standing = rules.standing_on(item, self._today(), cals)
        context = {**self.get_serializer_context(), "calibrations": cals}
        return Response(ste.TestEquipmentSerializer(item, context=context).data)


def register(router) -> None:
    router.register("test-equipment", TestEquipmentViewSet, basename="test-equipment")
