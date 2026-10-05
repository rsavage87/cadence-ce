"""
A work order's labor, parts, and notes (slice 19, part A), as the work order drawer records them: nested under the work order's id,
the same id as /api/v1/work-orders/<id>/. Each does what the drawer's Log time, Add part, Remove, and Add a note do on the web
(apps/web/views_wo_costs.py, apps/web/views.py wo_note), through the same services (apps.workorders.costs, add_note) with the same
levels (apps/workorders/permissions.py), checked server-side on every request.

GET    /api/v1/work-orders/<id>/labor/             the labor lines, oldest day first, with the hours, the total, the Settings rate a
                                                   new line is charged at, and whether this user may log time now (or why not).
                                                   Work orders View.
POST   /api/v1/work-orders/<id>/labor/             log time. Body: {"worked_on": "YYYY-MM-DD", "hours": "1.5", "technician": "<id>",
                                                   "rate": "95.00", "description": "..."}; worked_on and hours are required. Work
                                                   orders Edit. The technician defaults to the one assigned, else the user's own
                                                   technician record; vendor service time takes none (the vendor's time is the
                                                   vendor's) and is charged the vendor rate. A rate other than the Settings rate
                                                   needs Approve (403); blank, or the Settings rate itself, is anyone's, and a line
                                                   from someone without Approve is charged the Settings rate. 201 with the line.
DELETE /api/v1/work-orders/<id>/labor/<line id>/   remove a labor line. Work orders Edit. 204.
GET    /api/v1/work-orders/<id>/parts/             the part lines, with the total. Work orders View.
POST   /api/v1/work-orders/<id>/parts/             add a part. Body: {"description": "...", "part_number": "...", "quantity": "2",
                                                   "unit_cost": "42.00", "po_number": "..."}; description, quantity, and unit_cost
                                                   are required. Work orders Edit. 201 with the line.
DELETE /api/v1/work-orders/<id>/parts/<line id>/   remove a part line. Work orders Edit. 204.
GET    /api/v1/work-orders/<id>/notes/             the drawer's timeline, oldest first: status changes, notes, and labor and parts,
                                                   each {"at", "who", "text"}. Work orders View.
POST   /api/v1/work-orders/<id>/notes/             add a note. Body: {"text": "..."} (at most 1,000 characters; no patient
                                                   information). Work orders Edit. 201 with the note as the timeline shows it.

The rules are apps.workorders.costs': a closed work order is the record and a cancelled one takes nothing (400 saying why), hours
above 0 and at most 24 on a line and for one technician on one day, at most two decimal places (refused, never rounded), the day
never in the future nor outside the work order's open days, each line priced to the cent. A rule broken is a 400 keyed by the
field (several at once), a field the endpoint does not take is a 400 ("Unknown field."), another facility's work order or line is a
404, as is a line under another work order.

Scoped users (apps.workorders.scoping: a vendor technician sees their company's work orders, a clinical requester their unit's) reach
these for the work orders in their share, as the web's Log time, Add part, and notes admit them, at their usual levels: a work order
outside the share is a 404, as another facility's is. In the timeline, the number of a work order outside their share reads "another
work order", as in the drawer.
"""
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from apps.credentials.models import Technician
from apps.tenants.context import get_current_tenant
from apps.web.forms_wo_costs import money_text  # the drawer's reading of a typed amount ("$1,250.00"), so both doors read it alike
from apps.workorders import costs, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import LaborLine, PartLine, WorkOrder

from . import serializers_work as sw
from .base import ApiViewSet, _hours, _parse_day, _via_service

WORK_ORDER = r"work-orders/(?P<work_order_pk>[^/.]+)"


class _WorkOrderPart(ApiViewSet):
    module = wo_perms.MODULE

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:  # a superuser who has not picked a tenant has no work orders to read or record on
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")

    def work_order(self, work_order_pk) -> WorkOrder:
        """The work order as this user may see it, as the drawer's get_wo: another facility's, or one outside a scoped user's share,
        is a 404."""
        qs = WorkOrder.objects.select_related("asset", "assigned_to")
        return get_object_or_404(scoping.work_orders(self.request.user, qs), pk=work_order_pk)

    def body(self, serializer_class) -> dict:
        parsed = serializer_class(data=self.request.data)
        parsed.is_valid(raise_exception=True)
        return parsed.validated_data

    def recording(self, request, wo) -> dict:
        """What the drawer's Cost section says about adding lines: offered only to those who may record, while the work order takes
        lines, and the reason it does not shown only to those who could otherwise (views_wo_costs.costs_context)."""
        can_record = wo_perms.can_record_work(request.user)
        locked = costs.locked_reason(wo)
        return {"can_record": can_record and not locked, "locked": locked if can_record else ""}


# --- labor -----------------------------------------------------------------------------------------------------------------------

class WorkOrderLaborViewSet(_WorkOrderPart):
    write_level = wo_perms.RECORD_LEVEL
    delete_level = wo_perms.RECORD_LEVEL  # removing a line is recording work, as on the drawer (not deleting a record: Full)
    scoped_actions = frozenset({"list", "create", "destroy"})

    def list(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        lines = list(wo.labor_lines.select_related("technician").order_by("worked_on", "created_at"))
        return Response({
            "work_order": wo.number, "count": len(lines), "hours": _hours(sum((line.hours for line in lines), Decimal(0))),
            "total": sw.money(sum((costs.labor_amount(line) for line in lines), Decimal(0))),
            "settings_rate": sw.money(costs.default_rate(wo)), "can_set_rate": wo_perms.can_set_rate(request.user),
            **self.recording(request, wo),
            "results": sw.LaborLineSerializer(lines, many=True, context={"request": request}).data,
        })

    def create(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        d = self.body(sw.LaborBodySerializer)
        rate = self._rate(request, wo, d.get("rate"))
        line = _via_service(costs.add_labor, wo, by=request.user, worked_on=_parse_day(d.get("worked_on")), hours=d.get("hours"),
                            technician=self._technician(d.get("technician")), rate=rate, description=d.get("description") or "")
        line = LaborLine.objects.select_related("technician", "work_order").get(pk=line.pk)
        return Response(sw.LaborLineSerializer(line, context={"request": request}).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, work_order_pk=None, pk=None):
        wo = self.work_order(work_order_pk)
        line = get_object_or_404(LaborLine.objects, pk=pk, work_order=wo)
        _via_service(costs.remove_labor, line, by=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _rate(request, wo, value):
        """The drawer's rule (views_wo_costs._check_rate): a rate other than the Settings one needs work-order Approve (403). Blank,
        or the Settings rate itself, is anyone's, and someone without Approve is charged the Settings rate whatever they sent. A
        pasted "$1,250.00" is read as the drawer reads it (forms_wo_costs.money_text)."""
        text = money_text(value or "")
        if wo_perms.can_set_rate(request.user):
            return text or None
        if text:
            try:
                same = Decimal(text) == costs.default_rate(wo)
            except (InvalidOperation, ValueError):
                same = False
            if not same:
                raise PermissionDenied(f"Charging a rate other than the Settings rate ({costs.money_text(costs.default_rate(wo))} an hour) "
                                       "needs work-order Approve.")
        return None

    @staticmethod
    def _technician(value):
        """One of this facility's technicians by id; the service refuses one no longer active, and any at all on vendor service,
        saying why. Blank means the default (costs.default_technician). The drawer offers the active ones in a list; over the API
        an id that is not one of this facility's technicians is refused, never read as the default."""
        if not value:
            return None
        try:
            pk = uuid.UUID(str(value))
        except ValueError:
            pk = None
        tech = Technician.objects.filter(pk=pk).first() if pk else None
        if tech is None:
            raise DRFValidationError({"technician": ["Choose a technician from this facility."]})
        return tech


# --- parts -----------------------------------------------------------------------------------------------------------------------

class WorkOrderPartViewSet(_WorkOrderPart):
    write_level = wo_perms.RECORD_LEVEL
    delete_level = wo_perms.RECORD_LEVEL
    scoped_actions = frozenset({"list", "create", "destroy"})

    def list(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        lines = list(wo.part_lines.order_by("created_at"))
        return Response({
            "work_order": wo.number, "count": len(lines), "total": sw.money(sum((costs.part_amount(line) for line in lines), Decimal(0))),
            **self.recording(request, wo),
            "results": sw.PartLineSerializer(lines, many=True, context={"request": request}).data,
        })

    def create(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        d = self.body(sw.PartBodySerializer)
        line = _via_service(costs.add_part, wo, by=request.user, description=d.get("description") or "", quantity=d.get("quantity"),
                            unit_cost=money_text(d.get("unit_cost") or ""), part_number=d.get("part_number") or "",
                            po_number=d.get("po_number") or "")
        return Response(sw.PartLineSerializer(line, context={"request": request}).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, work_order_pk=None, pk=None):
        wo = self.work_order(work_order_pk)
        line = get_object_or_404(PartLine.objects, pk=pk, work_order=wo)
        _via_service(costs.remove_part, line, by=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- notes and the timeline ------------------------------------------------------------------------------------------------------

class WorkOrderNoteViewSet(_WorkOrderPart):
    write_level = wo_perms.NOTE_LEVEL
    scoped_actions = frozenset({"list", "create"})

    def list(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        entries = wo_services.timeline(wo)
        return Response({"work_order": wo.number, "count": len(entries), "can_note": request.user.has_level(wo_perms.MODULE, wo_perms.NOTE_LEVEL),
                         "results": sw.TimelineEntrySerializer(entries, many=True, context={"request": request}).data})

    def create(self, request, work_order_pk=None):
        wo = self.work_order(work_order_pk)
        d = self.body(sw.NoteBodySerializer)
        try:
            note = wo_services.add_note(wo, d.get("text") or "", by=request.user)
        except ValidationError as e:
            raise DRFValidationError({"text": e.messages}) from e
        entry = {"at": note.created_at, "who": note.author_name or str(note.author or "") or "Staff", "text": note.text}  # as timeline() tells it
        return Response({"id": str(note.id), **sw.TimelineEntrySerializer(entry, context={"request": request}).data}, status=status.HTTP_201_CREATED)


def register(router):
    router.register(f"{WORK_ORDER}/labor", WorkOrderLaborViewSet, basename="workorder-labor")
    router.register(f"{WORK_ORDER}/parts", WorkOrderPartViewSet, basename="workorder-part")
    router.register(f"{WORK_ORDER}/notes", WorkOrderNoteViewSet, basename="workorder-note")
