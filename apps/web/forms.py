"""
Input parsing for the web screens. Filters come from query strings and are validated against
the allowed values; anything unknown is dropped rather than raising. Choices that depend on the
tenant's data are built in __init__, never at class level (no tenant is in context at import time).
"""
import uuid

from django import forms

from apps.credentials.services import qualification, ranked_technicians
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.equipment.services import ACTIVE_STATUS_FILTER, SORTS, AssetFilters, FleetBucket, SupportFilter, service_vendor
from apps.workorders.models import Priority, WoStatus, WoType
from apps.workorders.services import UNASSIGNED, WorkOrderFilters

VENDOR = "vendor"


def parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _one_of(value, allowed) -> str:
    return value if value in allowed else ""


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
        "statuses": [(ACTIVE_STATUS_FILTER, "Active (not retired)"), *AssetStatus.choices],
        "risks": RiskClass.choices,
        "supports": SupportFilter.choices,
    }


def parse_asset_filters(params, options: dict) -> AssetFilters:
    return AssetFilters(
        q=params.get("q", "").strip()[:100],
        category=_one_of(params.get("category"), options["categories"]),
        status=_one_of(params.get("status"), [ACTIVE_STATUS_FILTER, *AssetStatus.values]),
        risk=_one_of(params.get("risk"), RiskClass.values),
        department=_one_of(params.get("dept"), options["departments"]),
        support=_one_of(params.get("support"), SupportFilter.values),
        overdue=params.get("overdue") == "1",
        bucket=_one_of(params.get("bucket"), FleetBucket.values),
        sort=_one_of(params.get("sort"), SORTS) or "tag",
        descending=params.get("dir") == "desc",
    )


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
