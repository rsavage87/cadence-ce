"""
REST API: devices, device models, departments, work orders, and Settings (slice 19 moved the other areas to their own modules:
apps/api/base.py lists them). Every view inherits TenantAPIMixin (apps/api/tenancy.py), which sets the tenant once DRF has
authenticated the session or the token. Every viewset resolves its queryset per request through the tenant-scoped manager;
never put `queryset = Model.objects.all()` on the class (it would be evaluated at import time with no tenant in context and stay
empty).

Slice 16: a scoped user (the vendor technician, the clinical requester; apps.workorders.scoping) is refused by every endpoint
except the actions a view names in `scoped_actions` (ModulePermission). Work orders and devices name theirs and narrow their
querysets to the user's share, so a row outside it is a 404, as another facility's is, and search and ordering only sort that share.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from rest_framework import routers, status
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.fields import BooleanField
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import DataScope, Level
from apps.credentials.models import Technician
from apps.credentials.services import qualified_technicians
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq_services
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel
from apps.facility import services as fac_services
from apps.incidents import permissions as inc_perms
from apps.incidents.models import Incident
from apps.pm import permissions as pm_perms
from apps.tenants.context import get_current_tenant
from apps.workorders import permissions as wo_perms
from apps.workorders import scoping
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

from . import serializers as s
from .base import TenantViewSet, _refuse_on_create, _via_service
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin


def _as_bool(value):
    """A boolean as a body sends it (JSON true or false, or a form's "true", "1", "off"...), or None when it is not one."""
    try:
        return BooleanField().to_internal_value(value)
    except DRFValidationError:
        return None


class APIRootView(TenantAPIMixin, routers.APIRootView):
    """The /api/v1/ index. It reads no tenant rows, but every API view sets the tenant the same way."""

    needs_facility = False


class EquipmentWrites:
    """Equipment writes need the levels in apps.equipment.permissions, the same doors as the Equipment screen. Deleting a row that
    work orders, requests, or devices still point at is a 400 saying so, not a 500."""

    in_use = "{} is in use, so it cannot be deleted."

    @property
    def write_level(self):
        return {"create": eq_perms.ADD_LEVEL, "change_status": eq_perms.STATUS_LEVEL}.get(self.action, eq_perms.EDIT_LEVEL)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError:
            raise DRFValidationError({"detail": self.in_use.format(instance)}) from None


class DepartmentViewSet(EquipmentWrites, TenantViewSet):
    model, module, serializer_class = Department, "equipment", s.DepartmentSerializer
    search_fields = ["name"]
    in_use = "{} has devices or service requests, so it cannot be deleted."

    def create(self, request, *args, **kwargs):
        """Through create_department: a name the facility already has, in any letter case, returns that department (200, not 201)."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _refuse_on_create(serializer.validated_data, ("cost_center",), "department")
        before = Department.objects.count()
        dept = _via_service(eq_services.create_department, serializer.validated_data["name"])
        created = Department.objects.count() > before
        return Response(self.get_serializer(dept).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def perform_update(self, serializer):
        """A rename through rename_department (unique in any letter case); the cost center is a plain field."""
        d = serializer.validated_data
        dept = serializer.instance
        if "name" in d:
            _via_service(eq_services.rename_department, dept, d["name"])
        if "cost_center" in d and d["cost_center"] != dept.cost_center:
            dept.cost_center = d["cost_center"]
            dept.save(update_fields=["cost_center"])


class DeviceModelViewSet(EquipmentWrites, TenantViewSet):
    model, module, serializer_class = DeviceModel, "equipment", s.DeviceModelSerializer
    search_fields = ["manufacturer", "model", "description", "category"]
    in_use = "{} has devices, so it cannot be deleted."
    CREATE_FIELDS = ("manufacturer", "model", "description", "category", "oem_pm_interval_months", "expected_life_years", "list_cost",
                     "oem_schedule_required")  # the CMS mark: create_device_model refuses it from anyone without Equipment Approve (a 403)
    SCORE_FIELDS = ("risk_function", "risk_physical", "risk_maintenance", "risk_incidents", "risk_reviewed_on")

    def _refuse_score(self):
        """The rubric's parts are set at /risk-score/ (apps/api/views_pm.py: Equipment Approve, the band sets the class), never here;
        a body that sends them is refused rather than saved without them."""
        sent = [f for f in self.SCORE_FIELDS if hasattr(self.request.data, "get") and f in self.request.data]
        if sent:
            where = "/api/v1/device-models/<id>/risk-score/"
            raise DRFValidationError({f: [f"A model's risk score is set at {where} (Equipment Approve)."] for f in sent})

    def perform_create(self, serializer):
        # Through create_device_model, which needs a risk class and never takes an AEM interval: that is set only by approving an
        # AEM proposal on the model's AEM tab (apps.pm.aem), never through the API.
        self._refuse_score()
        d = serializer.validated_data
        if d.get("aem_interval_months") not in (None, ""):
            raise DRFValidationError({"aem_interval_months": ["An AEM interval is set by approving an AEM proposal (PM schedule, PM library)."]})
        _refuse_on_create(d, ("pm_procedure",), "model")
        serializer.instance = _via_service(eq_services.create_device_model, risk_class=d.get("risk_class"), by=self.request.user,
                                           **{f: d[f] for f in self.CREATE_FIELDS if f in d})

    def perform_update(self, serializer):
        # Through update_device_model: create's rules (intervals, cost, a name unique in any letter case) hold on every change too.
        # A new risk class needs the risk level (Approve), as on the screen; sending back the current one is fine.
        self._refuse_score()
        d, dm, user = serializer.validated_data, serializer.instance, self.request.user
        risk = d.get("risk_class")
        if risk is not None and risk != dm.risk_class and not eq_perms.can_set_risk(user):
            raise PermissionDenied("Changing a model's risk class needs Equipment Approve.")
        new_procedure = "pm_procedure" in d and (d["pm_procedure"].pk if d["pm_procedure"] else None) != dm.pm_procedure_id
        if new_procedure and not pm_perms.can_edit_program(user):
            # Choosing the procedure is the PM program's (PM Edit), as on the model's Procedure tab, and recorded the same way.
            raise PermissionDenied("Choosing a model's PM procedure needs PM Edit.")
        with transaction.atomic():  # all of the change or none of it
            if new_procedure:
                from apps.pm.procedures import set_model_procedure

                _via_service(set_model_procedure, dm, d["pm_procedure"], by=user)
            _via_service(eq_services.update_device_model, dm, by=user,
                         **{f: v for f, v in d.items() if f in eq_services.MODEL_FIELDS and f != "pm_procedure"})


class AssetViewSet(EquipmentWrites, TenantViewSet):
    """Adding and editing go through apps.equipment.services (create_asset, update_asset), status changes through POST {id}/status/
    (set_status). An edit never changes the fields in FIXED_ON_UPDATE: a PUT or PATCH that changes one is refused with a 400 saying
    where it changes instead, while sending back the value a GET returned is fine.

    Slice 25: adding takes added_as, as Add device's "Already in use here" box: "new" (the default) or "existing"; "imported" is the
    importer's, refused in words. It never changes afterwards (FIXED_ON_UPDATE).

    Slice 26, incoming inspections: adding takes incoming_inspection ("" the default: the status given; "waiting": out of service,
    waiting for its incoming inspection, an Incoming inspection work order opened unassigned) and inspection_due (that work order's
    due date). A waiting device is new: added_as existing, a status, a last PM, or a next PM sent with "waiting" is refused in words,
    keyed by that field, rather than ignored. The inspection is then assigned and completed as any work order: POST
    /api/v1/work-orders/{id}/assign/ or take/, then transition to completed with inspection_result (only its pass puts the device in
    service and starts its PMs). An edit of a waiting device takes no next PM (update_asset's refusal, keyed next_pm_on), and the flag
    (awaiting_inspection) is read-only. POST {id}/use-before-inspection/ puts it in use before the inspection (Equipment Approve)."""

    model, module, serializer_class = Asset, "equipment", s.AssetSerializer
    search_fields = ["tag", "serial", "device_model__model", "device_model__manufacturer", "department__name", "contract__reference"]
    ordering_fields = ["tag", "next_pm_on", "installed_on", "acquisition_cost"]
    # Slice 16: a scoped user reads the devices in their share and, at the usual levels (Equipment Edit; Approve to retire), edits
    # only those. Adding and deleting devices stay closed to them (a new device has no work orders, so a company-scoped user could
    # never see it; inventory is the facility-wide roles' work), as do the qualified technicians (the facility's staff, not a device).
    # Read only, as on the web: a device's status and details are facility-wide (retiring cancels every open PM on it, the facility's
    # included), so a vendor's or a unit's share is to see devices, never to change them. Slice 26: so is putting a device in use
    # before its incoming inspection (use_before_inspection), which the drawer never offers them either.
    scoped_actions = frozenset({"list", "retrieve"})
    in_use = "{0.tag} has work orders or service requests, so it cannot be deleted; retire it instead."
    FIXED_ON_UPDATE = {
        "tag": "Asset tags never change: they are on the sticker and in links.",
        "status": "Use POST /api/v1/assets/{id}/status/ to change the status.",
        "contract": "Use POST /api/v1/contracts/{id}/add_assets/ or remove_asset/ to change the contract.",
        "last_pm_on": "The last PM date comes from completed PM work orders.",
        "added_as": "How a device was added is recorded when it is added and never changes.",  # slice 25
        # Slice 26: taken only when the device is added (CREATE_ONLY)
        "incoming_inspection": "A device waits for its incoming inspection from when it is added. To open an incoming inspection now, use "
                               "POST /api/v1/work-orders/ with type inspection.",
        "inspection_due": "Change the incoming inspection's due date on its work order: PATCH /api/v1/work-orders/{id}/ with due_on.",
    }
    # Slice 26: no device field holds these (the serializer takes them write-only), so on an edit a blank one is no change and any
    # value is refused.
    CREATE_ONLY = ("incoming_inspection", "inspection_due")
    # Slice 25: what a device added here can be (create_asset's added_as). Imported is the importer's (Settings, Import data).
    ADDED_HERE = (AddedAs.NEW, AddedAs.EXISTING)
    # Slice 26: what a device added waiting for its incoming inspection never comes with, in words, keyed by the field sent.
    NOT_WITH_WAITING = {
        "added_as": "A device already in use here does not wait for an incoming inspection: send added_as \"new\" with incoming_inspection "
                    "\"waiting\", or leave incoming_inspection blank.",
        "status": "A device waiting for its incoming inspection is added out of service and goes into service when its inspection passes: "
                  "leave status out.",
        "last_pm_on": "A new device's PM schedule starts when it passes its incoming inspection: leave last_pm_on out.",
        "next_pm_on": "A new device's PM schedule starts when it passes its incoming inspection: leave next_pm_on out.",
    }

    def get_queryset(self):
        return scoping.assets(self.request.user, Asset.objects.select_related("device_model", "department", "contract"))

    def perform_create(self, serializer):
        d = dict(serializer.validated_data)
        if d.pop("contract", None) is not None:
            raise DRFValidationError({"contract": ["Add the device first, then use POST /api/v1/contracts/{id}/add_assets/."]})
        d["added_as"] = d.get("added_as") or AddedAs.NEW  # new to the facility unless said otherwise (Add device's box unticked)
        if d["added_as"] not in self.ADDED_HERE:
            raise DRFValidationError({"added_as": ["Imported devices come in through Settings, Import data. A device added here is "
                                                   "\"new\" (new to the facility) or \"existing\" (already in use here)."]})
        self._check_incoming(d)
        serializer.instance = _via_service(eq_services.create_asset, by=self.request.user, **d)

    def _check_incoming(self, d):
        """Slice 26: what a device added waiting for its incoming inspection is sent with. Every field that contradicts "waiting" is
        refused at once, keyed by that field (create_asset names one at a time and would put a status sent with it aside), and an
        inspection due date is refused without "waiting" rather than dropped."""
        if d.get("incoming_inspection") != eq_services.INCOMING_WAITING:
            if d.get("inspection_due") is not None:
                raise DRFValidationError({"inspection_due": ["Only a device added waiting for its incoming inspection (incoming_inspection "
                                                             "\"waiting\") has an inspection due date."]})
            return
        sent = {"added_as": d["added_as"] == AddedAs.EXISTING, "status": "status" in d,
                "last_pm_on": d.get("last_pm_on") is not None, "next_pm_on": d.get("next_pm_on") is not None}
        errors = {f: [self.NOT_WITH_WAITING[f]] for f, given in sent.items() if given}
        if errors:
            raise DRFValidationError(errors)

    def perform_update(self, serializer):
        asset, d = serializer.instance, dict(serializer.validated_data)
        if scoping.is_scoped(self.request.user) and "department" in d and d["department"].pk != asset.department_id:
            # Slice 16: a scoped user edits devices inside their share; moving one to another unit is the facility-wide roles' call.
            raise PermissionDenied("Moving a device to another department needs a role that sees the whole facility.")
        errors = {}
        for field, message in self.FIXED_ON_UPDATE.items():
            if field not in d:
                continue
            sent = d.pop(field)
            if (sent not in (None, "")) if field in self.CREATE_ONLY else (sent != getattr(asset, field)):
                errors[field] = [message]
        # Slice 26: read-only (editable=False; only a passed incoming inspection clears it): a different value is refused rather than
        # dropped, so a client never thinks it was saved. Sending back what a GET returned is fine.
        body = self.request.data if hasattr(self.request.data, "get") else {}
        if "awaiting_inspection" in body and _as_bool(body["awaiting_inspection"]) is not asset.awaiting_inspection:
            errors["awaiting_inspection"] = ["A device stops waiting for its incoming inspection when that inspection passes: complete it "
                                             "with POST /api/v1/work-orders/{id}/transition/ (status completed, inspection_result passed)."]
        if errors:
            raise DRFValidationError(errors)
        serializer.instance = _via_service(eq_services.update_asset, asset, by=self.request.user, **d)

    @action(detail=True, methods=["post"], url_path="status", url_name="status")
    def change_status(self, request, pk=None):
        """Body: {"to": "<status>", "note": "..."}. The drawer's status buttons: Equipment Edit, and Approve to retire or reinstate."""
        asset = self.get_object()
        body = request.data if hasattr(request.data, "get") else {}  # a JSON list or scalar body has no fields
        to = body.get("to")
        if to not in AssetStatus.values:
            raise DRFValidationError({"to": [f"Required; one of {', '.join(AssetStatus.values)}."]})
        if not eq_perms.can_set_status(request.user, asset.status, to):
            raise PermissionDenied("Retiring or reinstating a device needs Approve access.")
        _via_service(eq_services.set_status, asset, to, by=request.user, note=str(body.get("note") or ""))
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=["post"], url_path="use-before-inspection", url_name="use-before-inspection")
    def use_before_inspection(self, request, pk=None):
        """Slice 26, the drawer's "Put in use before inspection": body {"reason": "<UseBeforeInspection value>"} (no free text) puts a
        device waiting for its incoming inspection in service before it, through equipment.services.use_before_inspection. It keeps
        waiting (no next PM: its PMs start when the inspection passes), and its inspection goes to high priority, due the next day.
        Equipment Approve (permissions.can_use_before_inspection, the level retiring needs), and never a scoped user (not in
        scoped_actions). A refusal is a 400 in the service's words: keyed reason for a reason not listed, else as detail."""
        if not eq_perms.can_use_before_inspection(request.user):
            raise PermissionDenied(eq_services.USE_BEFORE_PERMISSION)
        asset = self.get_object()
        body = request.data if hasattr(request.data, "get") else {}  # a JSON list or scalar body has no fields
        reason = body.get("reason")
        asset = _via_service(eq_services.use_before_inspection, asset, reason if isinstance(reason, str) else "", by=request.user)
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=["get"])
    def qualified_technicians(self, request, pk=None):
        asset = self.get_object()
        data = [{"technician": t.name, "technician_id": str(t.id), "via": q.via, "expiring": q.expiring} for t, q in qualified_technicians(asset)]
        return Response(data)


class WorkOrderViewSet(TenantViewSet):
    model, module, serializer_class = WorkOrder, "workorders", s.WorkOrderSerializer
    search_fields = ["number", "legacy_number", "asset__tag", "problem", "requester"]
    ordering_fields = ["opened_on", "due_on", "priority"]
    # Slice 16: a scoped user reads and works (at the usual levels) only the work orders in their share. Creating is refused to a
    # company-scoped user (create below) and takes only devices in the share; deleting stays closed to them.
    # As on the web: scoped users read their share and move their work orders through the lifecycle (transition, which completes
    # through apps.workorders.completion). Opening, editing, and assigning stay the facility's: the web refuses them too (the choices
    # are the facility's devices and technicians). Leaving create/update out also keeps the browsable API's forms closed to them.
    # Slice 25: why a PM was late (late_reason), on the work orders in their share, as on the drawer.
    scoped_actions = frozenset({"list", "retrieve", "transition", "late_reason"})

    def get_queryset(self):
        qs = WorkOrder.objects.select_related("asset", "assigned_to").prefetch_related("labor_lines", "part_lines")
        return scoping.work_orders(self.request.user, qs)

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        if "data" in kwargs and scoping.is_scoped(self.request.user):
            # Slice 16: a scoped user opens or moves work only onto a device in their share; another device's id reads as unknown (400).
            serializer.fields["asset"].queryset = scoping.assets(self.request.user)
        return serializer

    def create(self, request, *args, **kwargs):
        if scoping.scope_of(request.user) == DataScope.COMPANY:
            # The vendor technician "sees and updates" the work assigned to their company; the facility opens and assigns it.
            raise PermissionDenied("Your role updates the work orders assigned to your company; the facility opens them.")
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        d = serializer.validated_data
        if (d.get("assigned_to") or d.get("vendor_service")) and not wo_perms.can_assign(self.request.user):
            raise PermissionDenied("Assigning work orders needs Approve access.")
        # Slice 26: a refusal is a 400 in the service's words (keyed type: a PM, or a second open incoming inspection, on a device
        # waiting for its incoming inspection), never a 500.
        wo = _via_service(wo_services.create_work_order, asset=d["asset"], type=d.get("type", "repair"), priority=d.get("priority", "normal"),
                          problem=d["problem"], requester=d.get("requester", ""), assigned_to=d.get("assigned_to"),
                          vendor_service=d.get("vendor_service", False), vendor_name=d.get("vendor_name", ""), due_on=d.get("due_on"),
                          created_by=self.request.user, tag_out=d.get("tagged_out", False))
        serializer.instance = wo

    ASSIGNMENT_FIELDS = ("assigned_to", "vendor_service", "vendor_name")

    def perform_update(self, serializer):
        # Assignment goes through the assign action so it is permission-checked and lands in the status history.
        wo, d = serializer.instance, serializer.validated_data
        if any(f in d and d[f] != getattr(wo, f) for f in self.ASSIGNMENT_FIELDS):
            raise DRFValidationError({"assigned_to": "Use POST /api/v1/work-orders/{id}/assign/ to change the assignment."})
        # Slice 15: what was done is recorded by completing (transition to completed, apps.workorders.completion), and a completed
        # or closed work order is the record: its fields stay as they are until it is reopened. A device with labor or parts
        # already on its work order keeps them (they are that device's cost).
        if "resolution" in self.request.data:
            raise DRFValidationError({"resolution": ["The resolution is recorded when the work order is completed (transition to completed)."]})
        # Slice 25: read-only here (the field is editable=False); a different value is refused rather than dropped, so a client never
        # thinks it was saved. Sending back what a GET returned is fine. Slice 26: an incoming inspection's result likewise.
        body = self.request.data if hasattr(self.request.data, "get") else {}
        sent = body.get("late_reason")
        if sent is not None and sent != wo.late_reason:
            raise DRFValidationError({"late_reason": ["Use POST /api/v1/work-orders/{id}/late-reason/ to record why a PM was late."]})
        sent = body.get("inspection_result")
        if sent is not None and sent != wo.inspection_result:
            raise DRFValidationError({"inspection_result": ["An incoming inspection's result is recorded when it is completed: POST "
                                                            "/api/v1/work-orders/{id}/transition/ with status completed and inspection_result."]})
        changed = [f for f, v in d.items() if v != getattr(wo, f)]
        if changed and wo.status not in OPEN_STATUSES:
            raise DRFValidationError({"detail": f"{wo.number} is {wo.get_status_display().lower()}: what it records stays as it is. Reopen it first."})
        self._check_inspection_change(wo, d)
        if "asset" in changed and (wo.labor_lines.exists() or wo.part_lines.exists()):
            raise DRFValidationError({"asset": [f"{wo.number} has labor or parts on file for {wo.asset.tag}; it stays with that device."]})
        serializer.save()

    @staticmethod
    def _check_inspection_change(wo, d):
        """Slice 26: an edit of an open work order never makes or unmakes an incoming inspection, moves one off or onto a device
        waiting for its incoming inspection, or puts a PM on a waiting device (its PMs start when it passes). Those are opened as their
        own work orders (POST /api/v1/work-orders/, where create_work_order keeps a waiting device to one open inspection and no PM).
        Refused in words, keyed type or asset."""
        new_type, new_asset = d.get("type", wo.type), d.get("asset", wo.asset)
        type_changed, asset_changed = new_type != wo.type, new_asset.pk != wo.asset_id
        if type_changed and wo.type == WoType.INSPECTION:
            raise DRFValidationError({"type": [f"{wo.number} is an incoming inspection and stays one. Cancel it (transition to cancelled) and "
                                               "open the work order you need with POST /api/v1/work-orders/."]})
        if type_changed and new_type == WoType.INSPECTION:
            raise DRFValidationError({"type": [f"{wo.number} stays a {wo.get_type_display().lower()}. Open an incoming inspection as its own "
                                               "work order: POST /api/v1/work-orders/ with type inspection."]})
        if asset_changed and wo.type == WoType.INSPECTION and wo.asset.awaiting_inspection:
            raise DRFValidationError({"asset": [f"{wo.number} is {wo.asset.tag}'s incoming inspection: it stays with that device while the "
                                                "device waits for it."]})
        if asset_changed and wo.type == WoType.INSPECTION and new_asset.awaiting_inspection:
            raise DRFValidationError({"asset": [f"{new_asset.tag} is waiting for its incoming inspection: open that inspection on it with POST "
                                                "/api/v1/work-orders/ (type inspection) rather than moving another device's here."]})
        if new_type == WoType.PM and new_asset.awaiting_inspection and (type_changed or asset_changed):
            raise DRFValidationError({"asset" if asset_changed else "type": [f"{new_asset.tag} is waiting for its incoming inspection: its PMs "
                                                                             "start when it passes its incoming inspection."]})

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        wo = self.get_object()
        if not hasattr(request.data, "get"):  # a JSON list or scalar body has no fields
            raise DRFValidationError({"detail": "Send the fields as a JSON object."})
        to_status = request.data.get("status")
        if not isinstance(to_status, str):  # a list or an object would reach the service's set lookup (a 500)
            raise DRFValidationError({"status": [f"Required; one of {', '.join(WoStatus.values)}."]})
        if not wo_perms.can_transition(request.user, wo.status, to_status):
            raise PermissionDenied("Closing or reopening a closed work order needs Approve access.")
        if to_status == WoStatus.COMPLETED:
            # As the drawer's Mark completed: the resolution, and for a PM its result and one {"result", "reading"} per checklist step.
            # Slice 26: an incoming inspection likewise, with inspection_result ("passed" or "failed"; required while its device waits
            # for it, when only a pass puts the device in service) and, optionally, its checklist's results (all of them or none).
            from apps.workorders.completion import complete_work_order

            d = request.data
            kwargs = {"resolution": d.get("resolution", ""), "pm_result": d.get("pm_result", ""), "results": d.get("results"),
                      "inspection_result": d.get("inspection_result", ""),
                      "late_reason": d.get("late_reason")}  # slice 25: why a PM completed after its due date was late, optional
            for flag in ("open_repair", "tag_out"):
                if flag in d:
                    kwargs[flag] = str(d[flag]).lower() in ("1", "true", "on", "yes")
            try:
                complete_work_order(wo, by=request.user, **kwargs)
            except ValidationError as e:
                return Response(e.message_dict if hasattr(e, "error_dict") else {"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(self.get_serializer(wo).data)
        try:
            wo_services.change_status(wo, to_status, by=request.user, note=request.data.get("note", ""))
        except ValidationError as e:
            # Slice 28: the service's words (a held device's refusal among them), keyed by field when it names one; never str(e),
            # which shows the list's brackets and quotes.
            return Response(e.message_dict if hasattr(e, "error_dict") else {"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        except KeyError:
            return Response({"detail": f"Cannot move {wo.number} from {wo.get_status_display()} to {to_status}."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(wo).data)

    def perform_destroy(self, instance):
        """Slice 28: an incident's investigation (Incident.work_order, PROTECT) stays with the incident's file, which is never deleted:
        a 400 in words, never a 500. The incident's number only for Incidents View (the delete needs Work orders Full, not that)."""
        try:
            instance.delete()
        except ProtectedError as e:
            numbers = sorted(o.number for o in e.protected_objects if isinstance(o, Incident))
            which = "an incident"
            if numbers and inc_perms.can_view(self.request.user):
                which = f"incident {numbers[0]}" if len(numbers) == 1 else f"incidents {', '.join(numbers)}"
            raise DRFValidationError({"detail": f"{instance.number} is the investigation of {which}, so it cannot be deleted: the "
                                                "incident's file keeps it."}) from None

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        wo = self.get_object()
        if not wo_perms.can_assign(request.user):
            raise PermissionDenied("Assigning work orders needs Approve access.")
        tech = Technician.objects.filter(pk=request.data.get("technician")).first() if request.data.get("technician") else None
        _via_service(wo_services.assign, wo, technician=tech, vendor_name=request.data.get("vendor_name", ""), by=request.user)  # a refusal is a 400
        return Response(self.get_serializer(wo).data)

    @action(detail=True, methods=["post"])
    def take(self, request, pk=None):
        """Slice 24, My work's Take: the caller takes this work order for their own technician profile (wo_services.take). Work orders
        Edit (ModulePermission's write level, as on the screen), and never a scoped user (not in scoped_actions). The rest is the
        service's: the facility's setting on, an open, unassigned, in-house work order on a device the caller is credentialed for;
        anything else is a 400 in words. No body."""
        wo = self.get_object()
        _via_service(wo_services.take, wo, by=request.user)
        return Response(self.get_serializer(wo).data)

    @action(detail=True, methods=["post"], url_path="late-reason", url_name="late-reason")
    def late_reason(self, request, pk=None):
        """Slice 25, the drawer's "Why late": body {"late_reason": "<LateReason value>"} ("" or null clears it) on a PM that missed its
        due date (apps.pm.services.missed_pms), through wo_services.set_late_reason. Work orders Edit, Approve once the work order is
        closed (permissions.can_set_late_reason); a scoped user only on the work orders in their share (get_object: a 404 for any other).
        A refusal is a 400 keyed late_reason in the service's words."""
        wo = self.get_object()
        if not wo_perms.can_set_late_reason(request.user, wo):
            raise PermissionDenied(f"{wo.number} is closed: recording why it was late now needs Work orders Approve.")
        body = request.data if hasattr(request.data, "get") else {}  # a JSON list or scalar body has no fields
        if "late_reason" not in body:
            raise DRFValidationError({"late_reason": ["Required: one of the reasons listed, or blank to clear it."]})
        value = body.get("late_reason")
        if value is not None and not isinstance(value, str):
            raise DRFValidationError({"late_reason": ["Choose one of the reasons listed."]})
        _via_service(wo_services.set_late_reason, wo, value or "", by=request.user)
        wo.refresh_from_db()
        return Response(self.get_serializer(wo).data)


class FacilitySettingsView(TenantAPIMixin, APIView):
    """GET the tenant's settings (defaults until first saved); PATCH any of them (Settings Edit). POST .../reset-policy/
    restores the default policy text. Every change goes through apps.facility.services, which validates and audits it.

    Slice 21: `time_zone` is the facility's time zone (an IANA name, e.g. "America/Chicago"; Tenant.timezone), set through
    set_time_zone like the Settings screen's Time zone panel. A PATCH is all or nothing, the time zone with the rest."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "settings"
    write_level = Level.EDIT
    read_only_fields = {"updated_at"}  # what GET returns but PATCH ignores, so a client can send back what it read
    ZONE = "time_zone"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:  # a superuser who has not picked a tenant would otherwise read or write nobody's settings
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")

    def _data(self):
        row = fac_services.get_settings()
        data = {f: getattr(row, f) for f in fac_services.EDITABLE}
        data["updated_at"] = None if row._state.adding else row.updated_at  # unsaved defaults have never been changed
        data[self.ZONE] = get_current_tenant().zone_name  # its own, or the server's while it has chosen none (set_time_zone updates it in place)
        return s.FacilitySettingsSerializer(data).data

    def get(self, request):
        return Response(self._data())

    def patch(self, request):
        parsed = s.FacilitySettingsSerializer(data=request.data, partial=True)
        parsed.is_valid(raise_exception=True)
        unknown = set(request.data) - set(fac_services.EDITABLE) - self.read_only_fields - {self.ZONE}
        if unknown:
            return Response({"detail": f"Unknown settings: {', '.join(sorted(unknown))}."}, status=status.HTTP_400_BAD_REQUEST)
        fields = dict(parsed.validated_data)
        zone = fields.pop(self.ZONE, None)
        try:
            with transaction.atomic():  # all or nothing: a refused setting undoes the time zone, and the other way round
                if zone is not None:
                    fac_services.set_time_zone(get_current_tenant(), zone, by=request.user)
                if fields or zone is None:
                    fac_services.update_settings(by=request.user, **fields)
        except ValidationError as e:
            get_current_tenant().refresh_from_db(fields=["timezone"])  # rolled back: what is on record, not the zone refused with it
            return Response(e.message_dict if hasattr(e, "error_dict") else {"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self._data())


class ResetPolicyView(FacilitySettingsView):
    def get(self, request):
        raise MethodNotAllowed("GET")

    def patch(self, request):
        raise MethodNotAllowed("PATCH")

    def post(self, request):
        fac_services.reset_policy(by=request.user)
        return Response(self._data())
