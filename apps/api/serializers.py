from rest_framework import serializers

from apps.contracts.models import Contract
from apps.credentials.models import Credential, Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import progress as recall_progress
from apps.workorders.models import LaborLine, PartLine, WorkOrder


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "cost_center"]


class DeviceModelSerializer(serializers.ModelSerializer):
    pm_interval_months = serializers.IntegerField(read_only=True)

    class Meta:
        model = DeviceModel
        fields = ["id", "manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "aem_interval_months",
                  "pm_interval_months", "expected_life_years", "list_cost", "pm_procedure"]


class AssetSerializer(serializers.ModelSerializer):
    device_model_detail = DeviceModelSerializer(source="device_model", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    contract_reference = serializers.CharField(source="contract.reference", read_only=True, default=None)
    under_contract = serializers.BooleanField(read_only=True)

    class Meta:
        model = Asset
        fields = ["id", "tag", "serial", "device_model", "device_model_detail", "department", "department_name", "room", "status", "installed_on",
                  "acquisition_cost", "condition", "warranty_end", "support_type", "contract", "contract_reference", "under_contract",
                  "last_pm_on", "next_pm_on", "notes", "updated_at"]
        read_only_fields = ["support_type"]


class LaborLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = LaborLine
        fields = ["id", "technician", "worked_on", "hours", "rate", "description"]


class PartLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartLine
        fields = ["id", "description", "part_number", "quantity", "unit_cost", "po_number"]


class WorkOrderSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.tag", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.name", read_only=True, default=None)
    labor_lines = LaborLineSerializer(many=True, read_only=True)
    part_lines = PartLineSerializer(many=True, read_only=True)
    is_late = serializers.BooleanField(read_only=True)
    total_cost = serializers.FloatField(read_only=True)

    class Meta:
        model = WorkOrder
        fields = ["id", "number", "asset", "asset_tag", "type", "priority", "status", "source", "requester", "callback", "reported_location",
                  "assigned_to", "assigned_to_name", "vendor_service", "vendor_name", "opened_on", "due_on", "started_on", "completed_on",
                  "problem", "resolution", "estimated_hours", "tagged_out", "is_late", "total_cost", "labor_lines", "part_lines", "updated_at"]
        read_only_fields = ["number", "status", "started_on", "completed_on"]


class ContractSerializer(serializers.ModelSerializer):
    device_count = serializers.SerializerMethodField()
    status = serializers.CharField(read_only=True)

    class Meta:
        model = Contract
        fields = ["id", "reference", "vendor", "type", "coverage", "start_on", "end_on", "annual_cost", "notes", "status", "device_count", "updated_at"]

    def get_device_count(self, obj):
        return obj.covered_assets().count()


class CredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = Credential
        fields = ["id", "technician", "scope", "value", "source", "issued_on", "expires_on", "status"]


class TechnicianSerializer(serializers.ModelSerializer):
    credentials = CredentialSerializer(many=True, read_only=True)

    class Meta:
        model = Technician
        fields = ["id", "user", "name", "title", "certification", "weekly_capacity_hours", "is_active", "credentials"]


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ["id", "source", "external_id", "classification", "manufacturer", "product", "model_terms", "title", "action", "url", "published_on"]


class AlertMatchSerializer(serializers.ModelSerializer):
    alert_detail = AlertSerializer(source="alert", read_only=True)
    affected_count = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()

    class Meta:
        model = AlertMatch
        fields = ["id", "alert", "alert_detail", "device_model", "status", "disposition_note", "closed_on", "affected_count", "progress"]
        # A match's identity is fixed by matching and closed_on by set_status; over PATCH only the note (and status via the transition action) change.
        read_only_fields = ["alert", "device_model", "closed_on"]

    def get_affected_count(self, obj):
        return obj.affected_assets().count()

    def get_progress(self, obj):
        p = recall_progress(obj)
        return {"total": p["total"], "completed": p["completed"]}


class FacilitySettingsSerializer(serializers.Serializer):
    """Parses types only; apps.facility.services validates ranges and text and writes the history."""

    portal_require_callback = serializers.BooleanField(required=False)
    portal_hotline = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    policy_life_support = serializers.CharField(required=False, allow_blank=True)
    policy_medium_low = serializers.CharField(required=False, allow_blank=True)
    policy_aem = serializers.CharField(required=False, allow_blank=True)
    policy_missing = serializers.CharField(required=False, allow_blank=True)
    policy_incoming = serializers.CharField(required=False, allow_blank=True)
    policy_post_repair = serializers.CharField(required=False, allow_blank=True)
    policy_assignment = serializers.CharField(required=False, allow_blank=True)
    policy_portal = serializers.CharField(required=False, allow_blank=True)
    target_pm_pct = serializers.DecimalField(max_digits=6, decimal_places=2, required=False)
    target_uptime_pct = serializers.DecimalField(max_digits=6, decimal_places=2, required=False)
    target_mttr_days = serializers.DecimalField(max_digits=6, decimal_places=2, required=False)
    repair_budget_monthly = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
