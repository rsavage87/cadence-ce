"""
REST API. Every viewset resolves its queryset per request through the tenant-scoped manager;
never put `queryset = Model.objects.all()` on the class (it would be evaluated at import time
with no tenant in context and stay empty).
"""
from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Level
from apps.contracts.models import Contract
from apps.credentials.models import Credential, Technician
from apps.credentials.services import qualified_technicians
from apps.equipment.models import Asset, Department, DeviceModel
from apps.pm.services import generate_pm_work_orders
from apps.recalls import permissions as rc_perms
from apps.recalls import services as rc_services
from apps.recalls.models import AlertMatch
from apps.reports.services import REPORTS, overview_kpis, report_meta, run_report
from apps.tenants.context import get_current_tenant
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import WorkOrder

from . import serializers as s
from .permissions import ModulePermission


class TenantViewSet(viewsets.ModelViewSet):
    model = None
    module = None
    permission_classes = [IsAuthenticated, ModulePermission]

    def get_queryset(self):
        return self.model.objects.all()

    def perform_create(self, serializer):
        serializer.save(tenant=get_current_tenant())


class DepartmentViewSet(TenantViewSet):
    model, module, serializer_class = Department, "equipment", s.DepartmentSerializer
    search_fields = ["name"]


class DeviceModelViewSet(TenantViewSet):
    model, module, serializer_class = DeviceModel, "equipment", s.DeviceModelSerializer
    search_fields = ["manufacturer", "model", "description", "category"]


class AssetViewSet(TenantViewSet):
    model, module, serializer_class = Asset, "equipment", s.AssetSerializer
    search_fields = ["tag", "serial", "device_model__model", "device_model__manufacturer", "department__name", "contract__reference"]
    ordering_fields = ["tag", "next_pm_on", "installed_on", "acquisition_cost"]

    def get_queryset(self):
        return Asset.objects.select_related("device_model", "department", "contract")

    @action(detail=True, methods=["get"])
    def qualified_technicians(self, request, pk=None):
        asset = self.get_object()
        data = [{"technician": t.name, "technician_id": str(t.id), "via": q.via, "expiring": q.expiring} for t, q in qualified_technicians(asset)]
        return Response(data)


class WorkOrderViewSet(TenantViewSet):
    model, module, serializer_class = WorkOrder, "workorders", s.WorkOrderSerializer
    search_fields = ["number", "asset__tag", "problem", "requester"]
    ordering_fields = ["opened_on", "due_on", "priority"]

    def get_queryset(self):
        return WorkOrder.objects.select_related("asset", "assigned_to").prefetch_related("labor_lines", "part_lines")

    def perform_create(self, serializer):
        d = serializer.validated_data
        if (d.get("assigned_to") or d.get("vendor_service")) and not wo_perms.can_assign(self.request.user):
            raise PermissionDenied("Assigning work orders needs Approve access.")
        wo = wo_services.create_work_order(asset=d["asset"], type=d.get("type", "repair"), priority=d.get("priority", "normal"), problem=d["problem"],
                                           requester=d.get("requester", ""), assigned_to=d.get("assigned_to"), vendor_service=d.get("vendor_service", False),
                                           vendor_name=d.get("vendor_name", ""), due_on=d.get("due_on"), created_by=self.request.user,
                                           tag_out=d.get("tagged_out", False))
        serializer.instance = wo

    ASSIGNMENT_FIELDS = ("assigned_to", "vendor_service", "vendor_name")

    def perform_update(self, serializer):
        # Assignment goes through the assign action so it is permission-checked and lands in the status history.
        wo, d = serializer.instance, serializer.validated_data
        if any(f in d and d[f] != getattr(wo, f) for f in self.ASSIGNMENT_FIELDS):
            raise DRFValidationError({"assigned_to": "Use POST /api/v1/work-orders/{id}/assign/ to change the assignment."})
        serializer.save()

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        wo = self.get_object()
        to_status = request.data.get("status")
        if not wo_perms.can_transition(request.user, wo.status, to_status):
            raise PermissionDenied("Closing or reopening a closed work order needs Approve access.")
        try:
            wo_services.change_status(wo, to_status, by=request.user, note=request.data.get("note", ""))
        except (ValidationError, KeyError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(wo).data)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        wo = self.get_object()
        if not wo_perms.can_assign(request.user):
            raise PermissionDenied("Assigning work orders needs Approve access.")
        tech = Technician.objects.filter(pk=request.data.get("technician")).first() if request.data.get("technician") else None
        wo_services.assign(wo, technician=tech, vendor_name=request.data.get("vendor_name", ""), by=request.user)
        return Response(self.get_serializer(wo).data)


class ContractViewSet(TenantViewSet):
    model, module, serializer_class = Contract, "contracts", s.ContractSerializer
    search_fields = ["reference", "vendor"]

    @action(detail=True, methods=["post"])
    def add_assets(self, request, pk=None):
        """Body: {"asset_ids": [...]} or {"device_model": "<id>"} to add every active device of a model."""
        contract = self.get_object()
        if request.data.get("device_model"):
            assets = Asset.objects.filter(device_model_id=request.data["device_model"], status__in=Asset.ACTIVE_STATUSES)
        else:
            assets = Asset.objects.filter(id__in=request.data.get("asset_ids", []))
        n = contract.add_assets(assets)
        return Response({"added": n, "device_count": contract.covered_assets().count()})

    @action(detail=True, methods=["post"])
    def remove_asset(self, request, pk=None):
        contract = self.get_object()
        asset = Asset.objects.filter(pk=request.data.get("asset_id")).first()
        if asset is None:
            return Response({"detail": "asset_id required"}, status=status.HTTP_400_BAD_REQUEST)
        contract.remove_asset(asset)
        return Response({"device_count": contract.covered_assets().count()})


class TechnicianViewSet(TenantViewSet):
    model, module, serializer_class = Technician, "users", s.TechnicianSerializer

    def get_queryset(self):
        return Technician.objects.prefetch_related("credentials")


class CredentialViewSet(TenantViewSet):
    # Removing a credential is part of routine credential upkeep, gated like adding one (the web tab does the same).
    model, module, serializer_class, delete_level = Credential, "users", s.CredentialSerializer, Level.EDIT


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


class OverviewViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ModulePermission]
    module = "reports"

    def list(self, request):
        from datetime import date

        today = date.today()
        year, month = int(request.query_params.get("y", today.year)), int(request.query_params.get("m", today.month))
        return Response(overview_kpis(year, month))


class ReportViewSet(viewsets.ViewSet):
    """The Reports screen's tables as JSON: the list names them, `/<key>/` returns one (columns and rows, as the CSV download)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "reports"

    def list(self, request):
        return Response([{"key": r["key"], "title": r["title"], "subtitle": r["subtitle"]} for r in REPORTS])

    def retrieve(self, request, pk=None):
        from datetime import date

        meta = report_meta(pk)
        if meta is None:
            raise NotFound("No such report")
        today = date.today()
        data = run_report(pk, today)
        return Response({"key": pk, "title": meta["title"], "as_of": today, "columns": data["columns"], "rows": data["rows"]})


class PmViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ModulePermission]
    module = "pm"
    write_level = Level.APPROVE

    @action(detail=False, methods=["post"])
    def generate(self, request):
        return Response({"created": generate_pm_work_orders()})
