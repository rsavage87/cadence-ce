from django.db import models
from django.db.models import Exists, OuterRef
from django.utils import timezone
from rest_framework import serializers

from apps.contracts.models import Contract
from apps.credentials.models import Credential, Technician
from apps.equipment import services as eq_services
from apps.equipment.models import AddedAs, Asset, Department, DeviceModel, Ownership
from apps.incidents.models import Incident
from apps.incidents.models import Status as IncidentStatus
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import progress as recall_progress
from apps.vendors import services as vendor_services
from apps.workorders import findings, repair_checks, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders.completion import checklist_of, checklist_signature, procedure_for, repair_outcomes
from apps.workorders.models import OPEN_STATUSES, LaborLine, PartLine, RepairCheck, WorkOrder, WoStatus, WoType

from .serializers_testequipment import test_equipment_of
from .serializers_vendors import engineer_choices


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


def open_inspections(assets, user=None) -> dict:
    """{device id: its open incoming inspection} for the devices among `assets` waiting for one (Asset.awaiting_inspection; slice 26):
    the earliest opened, as apps.workorders.inspections.open_inspection reads it. One a scoped user (apps.workorders.scoping) may not
    see is left out, as WorkOrderSerializer leaves out a follow_up_of outside the share. Two queries at most whatever the number of
    devices, the second only for a scoped user; none when no device waits."""
    waiting = [a.pk for a in assets if a.awaiting_inspection]
    if not waiting:
        return {}
    found: dict = {}
    rows = (WorkOrder.objects.filter(asset_id__in=waiting, type=WoType.INSPECTION, status__in=OPEN_STATUSES)
            .order_by("opened_on", "number").only("id", "number", "asset_id", "opened_on"))
    for wo in rows:
        found.setdefault(wo.asset_id, wo)
    if found and user is not None and scoping.is_scoped(user):
        shown = set(scoping.work_orders(user, WorkOrder.objects.filter(pk__in=[wo.pk for wo in found.values()])).values_list("pk", flat=True))
        found = {asset_id: wo for asset_id, wo in found.items() if wo.pk in shown}
    return found


def _plain(value):
    """A value sent or shown, for comparing the two: blank and null alike, everything else as text."""
    return None if value in (None, "") else str(value)


def stand_ins(assets, user=None) -> dict:
    """{device id: (id, tag) of the device of ours it stands in for} for the vendor loaners among `assets` (slice 29), as `user` may
    see it: a scoped user (apps.workorders.scoping) gets only one inside their share; another reads as none, so a loaner never names a
    device (or the repair it is out for) a vendor or a unit may not see. One query at most whatever the number of devices; none when
    no device stands in for one."""
    wanted = {a.pk: a.stands_in_for_id for a in assets if a.stands_in_for_id}
    if not wanted:
        return {}
    qs = Asset.objects.filter(pk__in=set(wanted.values()))
    if user is not None and scoping.is_scoped(user):
        qs = scoping.assets(user, qs)
    tags = dict(qs.values_list("pk", "tag"))
    return {pk: (str(ours), tags[ours]) for pk, ours in wanted.items() if ours in tags}


class AssetListSerializer(serializers.ListSerializer):
    """A page of devices: their open incoming inspections read in one go (open_inspections), not once per device, and the devices
    their vendor loaners stand in for (stand_ins, slice 29)."""

    def to_representation(self, data):
        items = list(data.all() if isinstance(data, models.manager.BaseManager) else data)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        found = open_inspections(items, user)
        self.context["_open_inspections"] = {a.pk: found.get(a.pk) for a in items}
        shown = stand_ins(items, user)
        self.context["_stand_ins"] = {a.pk: shown.get(a.pk) for a in items}
        return super().to_representation(items)


class AssetSerializer(serializers.ModelSerializer):
    """Parses and shows devices. Writes never save through it: AssetViewSet hands the parsed fields to create_asset, update_asset,
    and set_status (apps.equipment.services), which validate and record the history.

    added_as (slice 25): how the device came to be in Cadence (AddedAs), with added_as_label in words. Taken when the device is added
    ("new", the default, or "existing": already in use here; "imported" is the importer's, refused by AssetViewSet with words), never
    changed afterwards (AssetViewSet.FIXED_ON_UPDATE); blank for a device added before slice 25.

    Slice 26, incoming inspections: incoming_inspection and inspection_due are taken only when the device is added (write-only):
    "waiting" adds a new device out of service and waiting for its incoming inspection, with no next PM and an Incoming inspection
    work order opened unassigned (due inspection_due, else apps.workorders.inspections.INSPECTION_DUE_DAYS later); "" (the default)
    adds it in the status given. Read back: awaiting_inspection (read-only: only its passed inspection clears it), status_label (the
    status as the screens word it, equipment.services.status_label: "Awaiting inspection" for a device out of service waiting for
    it), and, while it waits, its open inspection: open_inspection (the work order's id, as follow_up_of is given) and
    open_inspection_number, both null for a scoped user outside whose share that work order is.

    Slice 28: incident_hold (read-only), held as evidence for an incident investigation (status_label then reads "Held for incident");
    only apps.incidents.services sets and clears it (POST /api/v1/incidents/, a hold's release). A different value sent is refused
    rather than dropped; sending back what a GET returned is fine. No incident number here: the devices API is Equipment View's.

    Slice 29, rentals, vendor loaners, and demo units, read only this slice: ownership (Ownership: "owned" for ours) with
    ownership_label, owner, owner_reference (the agreement, PO, or RMA number), arrived_on, due_back_on, owner_pm_due_on (the owner's PM
    date), returned_on, kept_on, and stands_in_for (a vendor loaner's device of ours, by id) with stands_in_for_tag, both null for a
    scoped user outside whose share that device is (stand_ins). A device added through the API is ours; the temporary ones are added
    and changed on the Equipment screen. A value sent for one of these other than what a GET returns is refused, keyed by the field,
    rather than dropped (TEMPORARY_FIELDS)."""

    HOLD_REFUSAL = ("A device is held as evidence by recording an incident (POST /api/v1/incidents/) and released by the incident "
                    "(POST /api/v1/incidents/{id}/holds/{hold}/release/).")
    TEMPORARY_FIELDS = ("ownership", "owner", "owner_reference", "arrived_on", "due_back_on", "owner_pm_due_on", "returned_on", "kept_on",
                        "stands_in_for")
    TEMPORARY_REFUSAL = ("Rentals, vendor loaners, and demo units are added and changed on the Equipment screen (Add rental or loaner, the "
                         "device's Change details, Return to owner, Keep it); a device added through the API is the facility's own.")

    device_model_detail = DeviceModelSerializer(source="device_model", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    contract_reference = serializers.CharField(source="contract.reference", read_only=True, default=None)
    under_contract = serializers.BooleanField(read_only=True)
    # The model field is editable=False (only create_asset writes it); declared here so the create can take it
    added_as = serializers.ChoiceField(choices=AddedAs.choices, required=False, allow_blank=True)
    added_as_label = serializers.CharField(source="get_added_as_display", read_only=True)
    status_label = serializers.SerializerMethodField()
    open_inspection = serializers.SerializerMethodField()
    open_inspection_number = serializers.SerializerMethodField()
    # create_asset's alone, no device field holds them: AssetViewSet refuses them on an edit (FIXED_ON_UPDATE)
    incoming_inspection = serializers.ChoiceField(choices=[("", "Added in the status given"),
                                                           (eq_services.INCOMING_WAITING, "Waiting for its incoming inspection")],
                                                  required=False, allow_blank=True, write_only=True)
    inspection_due = serializers.DateField(required=False, allow_null=True, write_only=True)
    # Slice 29: read only (the temporary services are their writers); the model's fields are editable=False
    ownership_label = serializers.CharField(source="get_ownership_display", read_only=True)
    stands_in_for = serializers.SerializerMethodField()
    stands_in_for_tag = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = ["id", "tag", "serial", "device_model", "device_model_detail", "department", "department_name", "room", "status", "status_label",
                  "awaiting_inspection", "incident_hold", "open_inspection", "open_inspection_number", "installed_on", "acquisition_cost",
                  "condition", "warranty_end", "support_type", "contract", "contract_reference", "under_contract", "last_pm_on", "next_pm_on",
                  "notes", "added_as", "added_as_label", "incoming_inspection", "inspection_due",
                  "ownership", "ownership_label", "owner", "owner_reference", "arrived_on", "due_back_on", "owner_pm_due_on", "returned_on",
                  "kept_on", "stands_in_for", "stands_in_for_tag", "updated_at"]
        read_only_fields = ["support_type", "awaiting_inspection", "incident_hold", "ownership", "owner", "owner_reference", "arrived_on",
                            "due_back_on", "owner_pm_due_on", "returned_on", "kept_on"]
        list_serializer_class = AssetListSerializer

    def validate(self, attrs):
        """Slice 28: incident_hold is read-only (the incidents' services are its one writer): a value other than the device's is a 400
        keyed incident_hold, never dropped in silence. Slice 29: so are the temporary fields (TEMPORARY_FIELDS), each keyed by itself."""
        data = self.initial_data if hasattr(self.initial_data, "get") else {}
        if "incident_hold" in data:
            try:
                sent = serializers.BooleanField().to_internal_value(data["incident_hold"])
            except serializers.ValidationError:
                sent = None
            if sent is not (self.instance.incident_hold if self.instance is not None else False):
                raise serializers.ValidationError({"incident_hold": [self.HOLD_REFUSAL]})
        changed = [name for name in self.TEMPORARY_FIELDS if name in data and _plain(data[name]) != _plain(self._temporary_value(name))]
        if changed:
            raise serializers.ValidationError({name: [self.TEMPORARY_REFUSAL] for name in changed})
        return attrs

    def _temporary_value(self, name):
        """What a GET returns for one of TEMPORARY_FIELDS: the device's, or a new device's (ours, no stay)."""
        obj = self.instance
        if name == "ownership":
            return obj.ownership if obj is not None else Ownership.OWNED
        if name == "stands_in_for":
            return self.get_stands_in_for(obj) if obj is not None else None
        value = getattr(obj, name) if obj is not None else None
        return value.isoformat() if hasattr(value, "isoformat") else value

    def _stand_in(self, obj):
        """(id, tag) of the device `obj` stands in for, as this request's user may see it, or None: from the page's one read
        (AssetListSerializer), else read for this device alone, once for both fields."""
        known = self.context.setdefault("_stand_ins", {})
        if obj.pk not in known:
            request = self.context.get("request")
            known[obj.pk] = stand_ins([obj], getattr(request, "user", None)).get(obj.pk)
        return known[obj.pk]

    def get_stands_in_for(self, obj):
        found = self._stand_in(obj)
        return found[0] if found else None

    def get_stands_in_for_tag(self, obj):
        found = self._stand_in(obj)
        return found[1] if found else None

    def get_status_label(self, obj) -> str:
        return eq_services.status_label(obj)

    def _open_inspection(self, obj):
        """The device's open incoming inspection as this request's user may see it, or None: from the page's one read
        (AssetListSerializer), else read for this device alone, once for both fields."""
        known = self.context.setdefault("_open_inspections", {})
        if obj.pk not in known:
            request = self.context.get("request")
            known[obj.pk] = open_inspections([obj], getattr(request, "user", None)).get(obj.pk)
        return known[obj.pk]

    def get_open_inspection(self, obj):
        wo = self._open_inspection(obj)
        return str(wo.pk) if wo is not None else None

    def get_open_inspection_number(self, obj):
        wo = self._open_inspection(obj)
        return wo.number if wo is not None else None


class LaborLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = LaborLine
        fields = ["id", "technician", "worked_on", "hours", "rate", "description"]


class PartLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartLine
        fields = ["id", "description", "part_number", "quantity", "unit_cost", "po_number"]


def finding_limits(rows) -> dict:
    """Slice 33: {pk: (investigation, from_pm)} for the open repairs among `rows`, what limits the findings each offers
    (findings.choices_for): an incident's investigation not recorded in error (findings.is_investigation), a repair opened from a
    failed PM (findings.found_on_pm). The same two reads as those, for a page at once: one query, none without an open repair."""
    ids = [w.pk for w in rows if w.type == WoType.REPAIR and w.status in OPEN_STATUSES]
    if not ids:
        return {}
    read = WorkOrder.objects.filter(pk__in=ids).annotate(
        investigation=Exists(Incident.objects.filter(work_order_id=OuterRef("pk")).exclude(status=IncidentStatus.IN_ERROR)),
        from_pm=Exists(WorkOrder.objects.filter(pk=OuterRef("follow_up_of_id"), type=WoType.PM)))
    return {pk: (investigation, from_pm) for pk, investigation, from_pm in read.values_list("pk", "investigation", "from_pm")}


class WorkOrderListSerializer(serializers.ListSerializer):
    """A page of work orders: what limits each open repair's findings read in one go (finding_limits, slice 33), not once per row; and
    the test equipment each recorded (slice 35: serializers_testequipment.test_equipment_of, at most four queries for the page, none for a
    reader who does not read test equipment)."""

    def to_representation(self, data):
        items = list(data.all() if isinstance(data, models.manager.BaseManager) else data)
        self.context["_finding_limits"] = finding_limits(items)
        self.context["_test_equipment"] = test_equipment_of(items, getattr(self.context.get("request"), "user", None))
        return super().to_representation(items)


class WorkOrderSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.tag", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.name", read_only=True, default=None)
    labor_lines = LaborLineSerializer(many=True, read_only=True)
    part_lines = PartLineSerializer(many=True, read_only=True)
    is_late = serializers.BooleanField(read_only=True)
    total_cost = serializers.FloatField(read_only=True)
    late_reason_label = serializers.CharField(source="get_late_reason_display", read_only=True)
    inspection_result_label = serializers.CharField(source="get_inspection_result_display", read_only=True)
    repair_check_label = serializers.CharField(source="get_repair_check_display", read_only=True)  # slice 30
    completion_checklist = serializers.SerializerMethodField()
    # Slice 31: the vendor's engineer the work order names (as this user may read it), and on a single work order the engineers this
    # user may name (get_engineer_choices). Written only by the transition to completed and POST {id}/vendor-engineer/.
    vendor_engineer = serializers.SerializerMethodField()
    vendor_engineer_name = serializers.SerializerMethodField()
    engineer_choices = serializers.SerializerMethodField()
    # Slice 33: what a repair found (RepairFinding; apps.workorders.findings), the facility's record: null for a scoped user and on
    # other types. Written only by the transition to completed and POST {id}/repair-finding/.
    repair_finding = serializers.SerializerMethodField()
    repair_finding_label = serializers.SerializerMethodField()
    # Slice 35: the test equipment the work order recorded, each with its words on the day of the work (apps.testequipment.rules): null
    # for a reader who does not read test equipment (a scoped user; without Equipment View). Written only by the transition to completed
    # and POST {id}/test-equipment/.
    test_equipment = serializers.SerializerMethodField()

    class Meta:
        model = WorkOrder
        list_serializer_class = WorkOrderListSerializer
        fields = ["id", "number", "asset", "asset_tag", "type", "priority", "status", "source", "requester", "callback", "reported_location",
                  "assigned_to", "assigned_to_name", "vendor_service", "vendor_name", "opened_on", "due_on", "started_on", "completed_on",
                  "problem", "resolution", "estimated_hours", "tagged_out", "is_late", "total_cost", "labor_lines", "part_lines", "updated_at",
                  "pm_result", "checklist_results", "follow_up_of", "legacy_number", "late_reason", "late_reason_label", "inspection_result",
                  "inspection_result_label", "repair_check", "repair_check_label", "checks_reference", "completion_checklist", "vendor_engineer",
                  "vendor_engineer_name", "engineer_choices", "repair_finding", "repair_finding_label", "test_equipment"]
        # Slice 15: what a completion recorded (transition to completed, apps.workorders.completion) is read here, never written.
        # Slice 23: so is an imported work order's number in the previous system (apps.workorders.legacy sets it, once).
        # Slice 25: so is why a PM was late (the model field is editable=False): POST {id}/late-reason/ or the completion records it.
        # Slice 26: so is an incoming inspection's result (InspectionResult, editable=False): only its completion records it.
        # Slice 30: so is how a repair ended (RepairCheck) and the vendor's service report number (both editable=False).
        # Slice 31: so is the vendor's engineer (editable=False; a method field here, as the user may read it), and the report number
        # is any vendor job's.
        # Slice 33: so is what a repair found (editable=False; method fields here, as the user may read it).
        # Slice 35: so is the test equipment it recorded (a method field: the uses, judged on the day of the work).
        read_only_fields = ["number", "status", "started_on", "completed_on", "resolution", "pm_result", "checklist_results", "follow_up_of",
                            "legacy_number", "late_reason", "inspection_result", "repair_check", "checks_reference"]

    def get_completion_checklist(self, wo):
        """Slice 30: what completing this repair takes, for a client building its transition to completed: the standard checks after a
        repair (completion.procedure_for: never the model's PM procedure) as steps {"text", "measure"} (measure: what to record, or
        null), the signature to send back with the results, the steps never "na" (numbered from 1), and the outcomes (repair_check, with
        their labels) this user may complete it with (completion.repair_outcomes: "vendor_report" only on vendor service, for a user who
        sees the whole facility). Null for any other type, and for a repair no longer open (its checks as recorded are in
        checklist_results). The checklist reads no rows: it is Cadence's own, and the user's scope is on their role.

        Slice 33, what the repair found (repair_finding): "findings", the values this user may send, each {"value", "label", "hint"}
        (findings.choices_for, in the select's order); "found_on_pm", true when the repair was opened from a failed PM (the PM caught
        it: choose what failed; never pm_preventable); "finding_required", true when nothing is recorded (or what is recorded may no
        longer be kept), so the transition must send one; false: left out, the finding recorded stays. A scoped user offered none
        ("findings" empty, not required): the facility recorded one they may not choose, and it stays as recorded. What limits the
        choices is read once for a list's page (finding_limits), else for this repair alone."""
        if wo.type != WoType.REPAIR or wo.status not in OPEN_STATUSES:
            return None
        procedure = procedure_for(wo)
        steps = checklist_of(procedure)
        user = getattr(self.context.get("request"), "user", None)
        return {"name": procedure.name, "steps": [{"text": text, "measure": measure} for text, measure in steps],
                "signature": checklist_signature(steps), "never_na": [n + 1 for n in repair_checks.NEVER_NA],
                "outcomes": [{"value": value, "label": RepairCheck(value).label} for value in repair_outcomes(wo, user)],
                **self._finding_part(wo, user)}

    def _finding_part(self, wo, user) -> dict:
        """Slice 33: completion_checklist's findings, found_on_pm, and finding_required for the open repair `wo` (completion's rule:
        a value one of findings.choices_for; a scoped user leaves the facility's finding as stored)."""
        known = self.context.setdefault("_finding_limits", {})
        if wo.pk not in known:
            known.update(finding_limits([wo]))
        investigation, from_pm = known[wo.pk]
        choices = findings.choices_for(wo, user, investigation=investigation, from_pm=from_pm)
        stored, scoped = wo.repair_finding, scoping.is_scoped(user)
        if findings.facility_holds(wo, user, investigation=investigation, from_pm=from_pm):
            choices = []  # the facility's: it stays as recorded (review fix: only while the repair's own limits allow it)
        keeps = bool(stored) and stored in findings.choices_for(wo, None if scoped else user, investigation=investigation, from_pm=from_pm)
        return {"findings": [{"value": v, "label": findings.label(v), "hint": findings.hint(v, choices)} for v in choices],
                "found_on_pm": from_pm, "finding_required": not keeps}

    def _finding_hidden(self, wo) -> bool:
        """Slice 33: no finding to read: another type, or a scoped user (the finding is the facility's record)."""
        return wo.type != WoType.REPAIR or scoping.is_scoped(self._user())

    def get_repair_finding(self, wo):
        """Slice 33: the finding recorded (a RepairFinding value; "" when none is), null for a scoped user and on other types."""
        return None if self._finding_hidden(wo) else wo.repair_finding

    def get_repair_finding_label(self, wo):
        """Slice 33: the finding in words; "Not recorded" on a completed or closed repair with none (one completed before findings
        were recorded, or imported: POST {id}/repair-finding/ records one), "" on an open or cancelled one with none; null as
        repair_finding is."""
        if self._finding_hidden(wo):
            return None
        if wo.repair_finding or wo.status in (WoStatus.COMPLETED, WoStatus.CLOSED):
            return findings.label(wo.repair_finding)
        return ""

    def get_test_equipment(self, wo):
        """Slice 35: [{"id", "tag", "used_on", "words"}] by tag, each item the work order recorded with its words on its own day, the day of the
        work it was recorded on (rules.use_words: "SA-03 (Fluke ESA612): calibration due Mar 31, 2027 on the day of this work", "(on
        file since ...)" when its certificate was entered later, "found out of tolerance on ..." with its review; a reopened one's keep
        their days); [] when none (a cancelled one's too). Null for a reader who does not read test equipment: a scoped user
        (never theirs), or one without Equipment View. A list's page is read at once (WorkOrderListSerializer), else this work order alone."""
        if "_test_equipment" not in self.context:
            self.context["_test_equipment"] = test_equipment_of([wo], self._user())
        known = self.context["_test_equipment"]
        if known is None:
            return None
        if wo.pk not in known:
            known.update(test_equipment_of([wo], self._user()))
        return known[wo.pk]

    def _user(self):
        return getattr(self.context.get("request"), "user", None)

    def _in_list(self) -> bool:
        return isinstance(self.parent, serializers.ListSerializer)

    def get_vendor_engineer(self, wo):
        """Slice 31: the id of the vendor's engineer the work order names (null: none named); for a scoped user, null too when they
        would read "Another company's engineer" (vendors.services.engineer_shown). Read from the row's select_related: no query."""
        if not wo.vendor_engineer_id:
            return None
        user = self._user()
        if user is not None and scoping.is_scoped(user) and vendor_services.engineer_shown(wo, user) == vendor_services.ANOTHER_COMPANY:
            return None
        return str(wo.vendor_engineer_id)

    def get_vendor_engineer_name(self, wo):
        """Slice 31: "Dana Kim (Philips)", or "Another company's engineer" for a scoped user; null when none is named."""
        return vendor_services.engineer_shown(wo, self._user()) or None

    def get_engineer_choices(self, wo):
        """Slice 31, on a single work order (left out of a list's rows: it reads rows): for vendor service not cancelled, the engineers
        this user may send as vendor_engineer (serializers_vendors.engineer_choices: the work order's vendor's, then, for a user who
        sees the whole facility, every other vendor's, with what is on file for each on this device), when they may name one now:
        completing it (an open work order, Work orders Edit) or POST {id}/vendor-engineer/ (permissions.can_set_vendor_engineer).
        Null otherwise. Besides these: null (not named on the vendor's report), or engineer_name (someone else, by name)."""
        user = self._user()
        if self._in_list() or user is None or not wo.vendor_service or wo.status == WoStatus.CANCELLED:
            return None
        completing = wo.status in OPEN_STATUSES and wo_perms.can_transition(user, wo.status, WoStatus.COMPLETED)
        if not (completing or wo_perms.can_set_vendor_engineer(user, wo)):
            return None
        today = self.context.get("today") or timezone.localdate()
        return engineer_choices(wo, user, today)

    def to_representation(self, instance):
        """As a scoped user (apps.workorders.scoping) may read it, the same as the web's drawer: the number of a work order outside
        their share reads "another work order" in the problem and the resolution, and a link to one (follow_up_of) is left out.
        Slice 31: a list's rows leave engineer_choices out (GET the work order for them)."""
        data = super().to_representation(instance)
        if self._in_list():
            data.pop("engineer_choices", None)
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
    # Slice 27: the PM completion window (apps.pm.windows); the service checks the kinds, the days, and how they go together
    pm_window_high = serializers.CharField(required=False, allow_blank=True)
    pm_window_high_days = serializers.IntegerField(required=False, allow_null=True)
    pm_window_other = serializers.CharField(required=False, allow_blank=True)
    pm_window_other_days = serializers.IntegerField(required=False, allow_null=True)
    fiscal_year_start_month = serializers.IntegerField(required=False)  # slice 36: 1 (January) to 12; the service checks it
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)
