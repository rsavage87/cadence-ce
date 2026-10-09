"""
Input parsing for the web screens. Filters come from query strings and are validated against
the allowed values; anything unknown is dropped rather than raising. Choices that depend on the
tenant's data are built in __init__, never at class level (no tenant is in context at import time).
"""
import uuid
from dataclasses import dataclass, replace
from datetime import date

from django import forms
from django.utils import timezone

from apps.credentials.services import qualification, ranked_technicians
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership, RiskClass
from apps.equipment.services import (
    ACTIVE_STATUS_FILTER,
    OWNED,
    SORTS,
    AssetFilters,
    FleetBucket,
    SupportFilter,
    filter_assets,
    owner_maintains,
    service_vendor,
)
from apps.workorders.models import Priority, WoStatus, WoType
from apps.workorders.services import UNASSIGNED, WorkOrderFilters, no_pm_message

VENDOR = "vendor"


def parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _one_of(value, allowed) -> str:
    return value if value in allowed else ""


# Slice 29, temporary equipment on the Equipment toolbar. The Status option "Returned to owner" is a temporary device gone back (status
# retired, not ours); "Retired" then means a device of ours retired. "Whose" narrows by ownership: ours (kept devices included), the
# temporary devices on site, each kind (on site or returned: the Status filter narrows), or those past their due back date.
RETURNED_STATUS_FILTER = "returned"
WHOSE_OURS, WHOSE_TEMPORARY, WHOSE_PAST_DUE = "ours", "temporary", "past_due"
WHOSE_CHOICES = [(WHOSE_OURS, "Ours"), (WHOSE_TEMPORARY, "Temporary on site"), (Ownership.RENTAL, "Rental"), (Ownership.LOANER, "Vendor loaner"),
                 (Ownership.DEMO, "Demo or evaluation unit"), (WHOSE_PAST_DUE, "Past due back")]


@dataclass
class EquipmentFilters(AssetFilters):
    """The Equipment list's filters: equipment.services.AssetFilters plus slice 29's Whose (equipment_assets applies it)."""
    whose: str = ""


def asset_filter_options(assets=None) -> dict:
    """The Equipment toolbar's choices. `assets` (a scoped user's devices, apps.workorders.scoping) limits the categories and
    departments to theirs, so the lists name no other unit; None offers the facility's."""
    models, departments = DeviceModel.objects.all(), Department.objects.all()
    if assets is not None:
        models = models.filter(pk__in=assets.values("device_model_id"))
        departments = departments.filter(pk__in=assets.values("department_id"))
    return {
        "categories": list(models.order_by("category").values_list("category", flat=True).distinct()),
        "departments": list(departments.values_list("name", flat=True)),
        "statuses": [(ACTIVE_STATUS_FILTER, "Active (not retired)"), *AssetStatus.choices, (RETURNED_STATUS_FILTER, "Returned to owner")],
        "risks": RiskClass.choices,
        "supports": SupportFilter.choices,
        "whose": WHOSE_CHOICES,
    }


def parse_asset_filters(params, options: dict) -> EquipmentFilters:
    return EquipmentFilters(
        q=params.get("q", "").strip()[:100],
        category=_one_of(params.get("category"), options["categories"]),
        status=_one_of(params.get("status"), [ACTIVE_STATUS_FILTER, *AssetStatus.values, RETURNED_STATUS_FILTER]),
        risk=_one_of(params.get("risk"), RiskClass.values),
        department=_one_of(params.get("dept"), options["departments"]),
        support=_one_of(params.get("support"), SupportFilter.values),
        overdue=params.get("overdue") == "1",
        bucket=_one_of(params.get("bucket"), FleetBucket.values),
        sort=_one_of(params.get("sort"), SORTS) or "tag",
        descending=params.get("dir") == "desc",
        whose=_one_of(params.get("whose"), [v for v, _ in WHOSE_CHOICES]),
    )


def equipment_assets(f: AssetFilters, today: date | None = None, qs=None):
    """The Equipment list (the screen, its CSV, and its labels): equipment.services.filter_assets with slice 29's two filters applied
    to the devices first. Status "Returned to owner" is a temporary device gone back (retired, not ours) and "Retired" a device of ours
    retired; Whose (EquipmentFilters.whose) narrows by ownership. Over `qs` when given (a scoped user's devices)."""
    today = today or timezone.localdate()
    devices = Asset.objects.all() if qs is None else qs
    if f.status == RETURNED_STATUS_FILTER:
        devices, f = devices.filter(status=AssetStatus.RETIRED).exclude(OWNED), replace(f, status="")
    elif f.status == AssetStatus.RETIRED:
        devices = devices.filter(OWNED)
    whose = getattr(f, "whose", "")
    if whose == WHOSE_OURS:
        devices = devices.filter(OWNED)
    elif whose == WHOSE_TEMPORARY:
        devices = devices.exclude(OWNED).exclude(status=AssetStatus.RETIRED)
    elif whose == WHOSE_PAST_DUE:
        devices = devices.exclude(OWNED).exclude(status=AssetStatus.RETIRED).filter(due_back_on__lt=today)
    elif whose in Ownership.values:
        devices = devices.filter(ownership=whose)
    return filter_assets(f, today, qs=devices)


def temporary_on_site(qs=None) -> int:
    """How many rentals, vendor loaners, and demo units are on site (not returned), among `qs` (a scoped user's devices) or the
    facility's: the Equipment summary's "N temporary on site" (fleet_summary's dict stays as it was)."""
    return (Asset.objects.all() if qs is None else qs).exclude(OWNED).exclude(status=AssetStatus.RETIRED).count()


def parse_work_order_filters(params, technician_ids: set[str]) -> WorkOrderFilters:
    open_values = params.getlist("open")
    return WorkOrderFilters(
        q=params.get("q", "").strip()[:100],
        type=_one_of(params.get("type"), WoType.values),
        status=_one_of(params.get("status"), WoStatus.values),
        assigned=_one_of(params.get("assigned"), technician_ids | {UNASSIGNED}),
        # Open-only defaults on. The form sends a hidden open=0 ahead of the checkbox, so the last value wins.
        open_only=open_values[-1] == "1" if open_values else True,
    )


def technician_choices(asset) -> list[tuple[str, str]]:
    """Assignment options: credentialed technicians first (✓, ⚠ if expiring), then the rest (✕), then vendor service."""
    choices = []
    if asset is None:
        return choices
    for tech, q in ranked_technicians(asset):
        if q.ok:
            label = f"{'⚠' if q.expiring else '✓'} {tech.name} ({q.via}{', expiring' if q.expiring else ''})"
        else:
            label = f"✕ {tech.name} (not credentialed)"
        choices.append((str(tech.id), label))
    choices.append((VENDOR, f"Vendor service ({vendor_name_for(asset)})"))
    return choices


def vendor_name_for(asset) -> str:
    """Who a work order's vendor service names (slice 29: equipment.services.service_vendor, a temporary device's owner first)."""
    return service_vendor(asset)


class NewWorkOrderForm(forms.Form):
    asset = forms.CharField(widget=forms.HiddenInput, required=False)
    type = forms.ChoiceField(choices=WoType.choices, initial=WoType.REPAIR)
    priority = forms.ChoiceField(choices=Priority.choices, initial=Priority.NORMAL)
    requester = forms.CharField(max_length=120, required=False, label="Requested by")
    assignee = forms.ChoiceField(required=False, label="Assign to")
    problem = forms.CharField(widget=forms.Textarea(attrs={"placeholder": "What was observed? Is the device still usable? No patient information."}),
                              label="Problem description", max_length=2000)
    tag_out = forms.BooleanField(required=False, label="Tag the device out of service")
    # Slice 24: a technician who may take work (apps.workorders.services.may_take_as) and is credentialed for the device is offered
    # this instead of Assign to, ticked; the view takes the new work order for them (services.take). Never with Assign to.
    take = forms.BooleanField(required=False, initial=True, label="Assign it to me")

    def __init__(self, *args, can_assign: bool, taker=None, **kwargs):
        """`taker`: the technician the user takes work as, or None (a manager, who assigns, or someone who takes none)."""
        super().__init__(*args, **kwargs)
        self.asset_obj = self._lookup_asset(self.data.get("asset") if self.is_bound else self.initial.get("asset"))
        if self.asset_obj is not None:
            self.fields["problem"].widget.attrs["autofocus"] = True
        if self.asset_obj is not None and owner_maintains(self.asset_obj):
            # Slice 29: a rental, vendor loaner, or demo unit gets no PM work orders (its owner maintains it; create_work_order refuses
            # one too). Posted anyway, the refusal names why.
            self.fields["type"].choices = [c for c in WoType.choices if c[0] != WoType.PM]
            self.fields["type"].error_messages["invalid_choice"] = no_pm_message(self.asset_obj)
        if can_assign:
            self.fields["assignee"].choices = [("", "Leave unassigned")] + technician_choices(self.asset_obj)
        else:
            del self.fields["assignee"]
        if can_assign or taker is None or self.asset_obj is None or not qualification(taker, self.asset_obj).ok:
            del self.fields["take"]

    @staticmethod
    def _lookup_asset(tag):
        if not tag:
            return None
        return Asset.objects.exclude(status=AssetStatus.RETIRED).select_related("device_model", "department", "contract").filter(tag=tag).first()

    def clean_asset(self):
        if self.asset_obj is None:
            raise forms.ValidationError("Choose a device from the list first.")
        return self.asset_obj
