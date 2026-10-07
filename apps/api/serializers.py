from rest_framework import serializers

from apps.contracts.models import Contract
from apps.credentials.models import Credential, Technician
from apps.equipment.models import AddedAs, Asset, Department, DeviceModel
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import progress as recall_progress
from apps.workorders import scoping
from apps.workorders.models import LaborLine, PartLine, WorkOrder


class DepartmentSerializer(serializers.ModelSerializer):
    """Adding one goes through apps.equipment.services.create_department (DepartmentViewSet.create); this parses and shows."""

    class Meta:
        model = Department
        fields = ["id", "name", "cost_center"]


class DeviceModelSerializer(serializers.ModelSerializer):
    """Adding one goes through apps.equipment.services.create_device_model (DeviceModelViewSet.perform_create); this parses and shows.

    oem_schedule_required (slice 18: CMS keeps imaging, radiologic, and medical laser equipment on the manufacturer's schedule) is
    set when adding a model or with PATCH or PUT, through create_device_model or update_device_model, which refuse a user without
    Equipment Approve (a 403, as the risk class)."""

    pm_interval_months = serializers.IntegerField(read_only=True)

    class Meta:
        model = DeviceModel
        fields = ["id", "manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "aem_interval_months",
                  "pm_interval_months", "oem_schedule_required", "expected_life_years", "list_cost", "pm_procedure"]


class AssetSerializer(serializers.ModelSerializer):
    """Parses and shows devices. Writes never save through it: AssetViewSet hands the parsed fields to create_asset, update_asset,
    and set_status (apps.equipment.services), which validate and record the history.

    added_as (slice 25): how the device came to be in Cadence (AddedAs), with added_as_label in words. Taken when the device is added
    ("new", the default, or "existing": already in use here; "imported" is the importer's, refused by AssetViewSet with words), never
    changed afterwards (AssetViewSet.FIXED_ON_UPDATE); blank for a device added before slice 25."""

    device_model_detail = DeviceModelSerializer(source="device_model", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    contract_reference = serializers.CharField(source="contract.reference", read_only=True, default=None)
    under_contract = serializers.BooleanField(read_only=True)
    # The model field is editable=False (only create_asset writes it); declared here so the create can take it
    added_as = serializers.ChoiceField(choices=AddedAs.choices, required=False, allow_blank=True)
    added_as_label = serializers.CharField(source="get_added_as_display", read_only=True)

    class Meta:
        model = Asset
        fields = ["id", "tag", "serial", "device_model", "device_model_detail", "department", "department_name", "room", "status", "installed_on",
                  "acquisition_cost", "condition", "warranty_end", "support_type", "contract", "contract_reference", "under_contract",
                  "last_pm_on", "next_pm_on", "notes", "added_as", "added_as_label", "updated_at"]
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
    late_reason_label = serializers.CharField(source="get_late_reason_display", read_only=True)

    class Meta:
        model = WorkOrder
        fields = ["id", "number", "asset", "asset_tag", "type", "priority", "status", "source", "requester", "callback", "reported_location",
                  "assigned_to", "assigned_to_name", "vendor_service", "vendor_name", "opened_on", "due_on", "started_on", "completed_on",
                  "problem", "resolution", "estimated_hours", "tagged_out", "is_late", "total_cost", "labor_lines", "part_lines", "updated_at",
                  "pm_result", "checklist_results", "follow_up_of", "legacy_number", "late_reason", "late_reason_label"]
        # Slice 15: what a completion recorded (transition to completed, apps.workorders.completion) is read here, never written.
        # Slice 23: so is an imported work order's number in the previous system (apps.workorders.legacy sets it, once).
        # Slice 25: so is why a PM was late (the model field is editable=False): POST {id}/late-reason/ or the completion records it.
        read_only_fields = ["number", "status", "started_on", "completed_on", "resolution", "pm_result", "checklist_results", "follow_up_of",
                            "legacy_number", "late_reason"]

    def to_representation(self, instance):
        """As a scoped user (apps.workorders.scoping) may read it, the same as the web's drawer: the number of a work order outside
        their share reads "another work order" in the problem and the resolution, and a link to one (follow_up_of) is left out."""
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not scoping.is_scoped(user):
            return data
        seen = self.context.setdefault("_scoped_numbers", {})
        for field in ("problem", "resolution"):
            if field in data:
                data[field] = scoping.shown_text(user, data[field], seen)
        if data.get("follow_up_of") and not scoping.can_see_work_order(user, instance.follow_up_of):
            data["follow_up_of"] = None
        return data


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
    portal_confirmation = serializers.CharField(required=False, allow_blank=True)  # "screen" or "email" (slice 13)
    portal_email_domains = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
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
    labor_rate = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)  # slice 15; the service checks the range
    vendor_labor_rate = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    time_zone = serializers.CharField(required=False, allow_blank=True)  # slice 21: the facility's IANA time zone; set_time_zone checks it
    technicians_take_work = serializers.BooleanField(required=False)  # slice 24: technicians may take unassigned work they are credentialed for
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
