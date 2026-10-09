"""
Input parsing for Add device and Edit details (slice 12). Model and department choices depend on the tenant, so they are built in
__init__, never at class level. The rules (tags, dates, costs, a new model's fields) live in apps.equipment.services; these forms
parse what was typed and put the services' errors back on the fields they name.

Slice 26, incoming inspections: Add device asks how the device arrives (`intake`, replacing slice 25's status radio and "Already in use
here" box). New and waiting for its incoming inspection (out of service, no next PM, an inspection opened: its due date and, for whoever
may, "Assign the inspection to me" or an inspector); new and inspected now (the same, with the inspection assigned to the user, whose Mark
completed comes next); or already in use here (in or out of service, its last and next PM, its install date required). Each choice's
fields show only while it is chosen (CSS :has), and clean() drops the other choices' values. Edit details on a device waiting for its
inspection shows its next PM as text: its PM schedule starts when the inspection passes.
"""
from dataclasses import dataclass
from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.credentials.models import Technician
from apps.equipment.models import AddedAs, AssetStatus, Department, DeviceModel, RiskClass, UseBeforeInspection
from apps.equipment.services import EDITABLE_FIELDS, INCOMING_WAITING, owner_maintains
from apps.workorders.inspections import INSPECTION_DUE_DAYS

from .forms import VENDOR

NEW = "new"  # the model or department select's value that reveals the fields for adding one
CONDITIONS = [(1, "1 · Poor"), (2, "2 · Fair"), (3, "3 · Good"), (4, "4 · Very good"), (5, "5 · Excellent")]
STATUSES = [(AssetStatus.IN_SERVICE, "In service"), (AssetStatus.OUT_OF_SERVICE, "Out of service")]  # equipment already in use here
# How a device arrives (slice 26): Add device's `intake`, with what each choice says under it.
WAITING, INSPECT_NOW, EXISTING = "waiting", "inspect", "existing"
INTAKE_CHOICES = [(WAITING, "New: waiting for its incoming inspection"), (INSPECT_NOW, "New: inspect it now"),
                  (EXISTING, "Already in use here (existing equipment)")]
INTAKE_HINTS = {
    WAITING: "Out of service until its incoming inspection passes; its PM schedule starts on the day it does.",
    INSPECT_NOW: "Added waiting, with the inspection assigned to you: record it next, with its checklist and readings.",
    EXISTING: "Entered after the fact: no incoming inspection, and its install date is required. One installed in the last 30 days is "
              "probably new, and the survey binder asks about it.",
}
# Each choice's own fields; clean() drops the other choices' (hidden on screen, whatever the browser kept in them).
INTAKE_FIELDS = {WAITING: ("inspection_due", "take_inspection", "inspector"), INSPECT_NOW: (), EXISTING: ("status", "last_pm_on", "next_pm_on")}
# A new model's fields, named as apps.equipment.services.create_device_model takes them (and keys its errors).
MODEL_FIELDS = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "expected_life_years", "list_cost",
                "oem_schedule_required")
NOTES_PLACEHOLDER = "Accessories, mounting, history. No patient information."
# The order web/_asset_fields.html lays the fields out in, so a form sent back with errors focuses the first one on screen.
LAYOUT = ("tag", "serial", "device_model", *MODEL_FIELDS, "department", "room", "new_department", "installed_on", "warranty_end", "acquisition_cost",
          "condition", "intake", "inspection_due", "take_inspection", "inspector", "status", "last_pm_on", "next_pm_on", "notes")


@dataclass(frozen=True)
class IntakeOffer:
    """What Add device offers one user for a new device's incoming inspection (views_equipment.intake_offer works it out). The
    inspection is opened unassigned; these are the ways it is then assigned, each through the work order services' own door."""
    assign: bool = False  # Work orders Approve: an inspector select (a technician or vendor service), through services.assign
    take: bool = False  # anyone else services.may_take_as allows: "Assign the inspection to me", through services.take
    inspect_now: bool = False  # may give it to themselves (take, or assign for Approve) and complete it in one step: "New: inspect it now"


def _date(label, help_text=""):
    return forms.DateField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


def _money(label, required, help_text=""):
    return forms.DecimalField(label=label, required=required, max_digits=12, decimal_places=2, help_text=help_text,
                              widget=forms.NumberInput(attrs={"min": 0, "step": "0.01"}))


def model_label(dm) -> str:
    return f"{dm.manufacturer} {dm.model} · {dm.description}"


class DeviceForm(forms.Form):
    """What Add device and Edit details share: the details update_asset can change (EDITABLE_FIELDS), each under its own name."""

    allow_new = False  # Add device offers "Add a new model" and "Add a new department" in the selects
    service_fields: dict[str, str] = {}  # a service error's key -> the field that shows it, where they differ

    serial = forms.CharField(label="Serial number", required=False, max_length=80)
    device_model = forms.ChoiceField(label="Model", error_messages={"required": "Choose a model.", "invalid_choice": "Choose a model from the list."})
    department = forms.ChoiceField(label="Department",
                                   error_messages={"required": "Choose a department.", "invalid_choice": "Choose a department from the list."})
    room = forms.CharField(label="Room", required=False, max_length=40)
    installed_on = _date("Installed on")
    warranty_end = _date("Warranty ends")
    condition = forms.TypedChoiceField(label="Condition", coerce=int, choices=CONDITIONS, initial=3,
                                       error_messages={"invalid_choice": "Condition is 1 (poor) to 5 (excellent)."})
    notes = forms.CharField(label="Notes", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2, "placeholder": NOTES_PLACEHOLDER}))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "dv-%s")
        super().__init__(*args, **kwargs)
        self.models = {str(m.id): m for m in DeviceModel.objects.all()}
        self.departments = {str(d.id): d for d in Department.objects.all()}
        self.fields["device_model"].choices = [("", "Choose a model"), *((pk, model_label(m)) for pk, m in self.models.items()),
                                               *([(NEW, "Add a new model")] if self.allow_new else [])]
        self.fields["department"].choices = [("", "Choose a department"), *((pk, d.name) for pk, d in self.departments.items()),
                                             *([(NEW, "Add a new department")] if self.allow_new else [])]
        today = timezone.localdate().isoformat()
        for name in ("installed_on", "last_pm_on"):  # the services refuse future dates; the date picker can say so first
            if name in self.fields:
                self.fields[name].widget.attrs["max"] = today

    def clean_device_model(self):
        value = self.cleaned_data["device_model"]
        return None if value == NEW else self.models[value]  # None: add the model typed below

    def clean_department(self):
        value = self.cleaned_data["department"]
        return None if value == NEW else self.departments[value]

    def add_service_errors(self, error):
        """Show a service's ValidationError on the fields it names (through service_fields where this form calls them something
        else); anything the form has no field for goes at the top. Never add_error(None, e) with a dict: an unknown key raises."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            field = self.service_fields.get(key, key)
            self.add_error(field if field in self.fields else None, messages)

    def focus_first_error(self):
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so make that the first field with an error."""
        first = next((name for name in LAYOUT if name in self.fields and name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True


class NewDeviceForm(DeviceForm):
    allow_new = True

    # Not required here: create_asset says what a tag needs. The browser still asks for one.
    tag = forms.CharField(label="Asset tag", required=False,
                          widget=forms.TextInput(attrs={"autofocus": True, "required": True, "maxlength": 40, "placeholder": "e.g. CE-10241",
                                                        "autocomplete": "off", "spellcheck": "false"}))
    acquisition_cost = _money("Acquisition cost, $", False, "Blank uses the model's list cost.")
    # Slice 26: how the device arrives (create_asset's added_as and incoming_inspection), and each choice's own fields (INTAKE_FIELDS)
    intake = forms.ChoiceField(label="How it arrives", choices=INTAKE_CHOICES, initial=WAITING, widget=forms.RadioSelect,
                               error_messages={"required": "Say how the device arrives.", "invalid_choice": "Choose one of the ways listed."})
    inspection_due = _date("Inspection due", f"{INSPECTION_DUE_DAYS} days from today unless you change it; a vendor's install can take longer.")
    take_inspection = forms.BooleanField(label="Assign the inspection to me", required=False, initial=True,
                                         help_text="If you are credentialed for the device; if not, it stays unassigned for a CE manager to assign.")
    inspector = forms.ChoiceField(label="Inspector", required=False, error_messages={"invalid_choice": "Choose an inspector from the list."})
    status = forms.ChoiceField(label="Status", choices=STATUSES, initial=AssetStatus.IN_SERVICE)
    last_pm_on = _date("Last PM")
    next_pm_on = _date("Next PM", "Blank lets the schedule work it out from the last PM or the install date.")
    # The new model, used only when the model select says "Add a new model"; create_device_model checks them.
    manufacturer = forms.CharField(label="Manufacturer", required=False, max_length=120)
    model = forms.CharField(label="Model name or number", required=False, max_length=120)
    description = forms.CharField(label="Description", required=False, max_length=200,
                                  widget=forms.TextInput(attrs={"placeholder": "Plain-language name, e.g. Infusion pump"}))
    category = forms.CharField(label="Category", required=False, max_length=80,
                               widget=forms.TextInput(attrs={"list": "dv-categories", "placeholder": "e.g. Infusion pumps", "autocomplete": "off"}))
    risk_class = forms.ChoiceField(label="Risk class", required=False, choices=RiskClass.choices, initial=RiskClass.MEDIUM)
    oem_pm_interval_months = forms.IntegerField(label="OEM PM interval, months", required=False, initial=12,
                                                widget=forms.NumberInput(attrs={"min": 1, "max": 120}))
    expected_life_years = forms.IntegerField(label="Expected life, years", required=False, initial=8, widget=forms.NumberInput(attrs={"min": 1, "max": 50}))
    list_cost = _money("List cost, $", False)
    # The CMS mark (slice 18) is Equipment Approve's (apps.equipment.permissions): the field exists only on their form, so anyone
    # else's new model starts unmarked whatever is posted (create_device_model refuses the mark from them too).
    oem_schedule_required = forms.BooleanField(
        label="Manufacturer's schedule required (CMS)", required=False,
        help_text="Imaging (diagnostic or therapeutic), radiologic, or medical laser equipment: CMS requires the manufacturer's maintenance "
                  "schedule, so the model never goes on AEM.")
    # The new department, used only when the department select says "Add a new department".
    new_department = forms.CharField(label="New department name", required=False, max_length=80)
    # create_asset's added_as (slice 25) and incoming_inspection (slice 26) both come from the intake choice
    service_fields = {"added_as": "intake", "incoming_inspection": "intake"}

    def __init__(self, *args, can_set_oem_schedule: bool = False, offer: IntakeOffer | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_set_oem_schedule = can_set_oem_schedule
        if not can_set_oem_schedule:
            del self.fields["oem_schedule_required"]
        # The category box suggests the facility's categories (a datalist); any new one is allowed.
        self.categories = list(DeviceModel.objects.order_by("category").values_list("category", flat=True).distinct())
        self.offer = offer = offer or IntakeOffer()
        if not offer.inspect_now:
            self.fields["intake"].choices = [c for c in INTAKE_CHOICES if c[0] != INSPECT_NOW]
        if not offer.take:
            del self.fields["take_inspection"]
        if offer.assign:  # the facility's active technicians (assign checks credentials and notes an override), or the vendor
            self.technicians = {str(t.pk): t for t in Technician.objects.filter(is_active=True)}
            self.fields["inspector"].choices = [("", "Leave unassigned"), *((pk, t.name) for pk, t in self.technicians.items()),
                                                (VENDOR, "Vendor service (the manufacturer's field service)")]
        else:
            del self.fields["inspector"]
        today = timezone.localdate()
        self.fields["inspection_due"].initial = today + timedelta(days=INSPECTION_DUE_DAYS)
        self.fields["inspection_due"].widget.attrs["min"] = today.isoformat()  # create_asset refuses a day before the device is added

    def clean_inspector(self):
        value = self.cleaned_data.get("inspector", "")
        return self.technicians[value] if value and value != VENDOR else value  # a Technician, VENDOR, or "" (unassigned)

    def clean(self):
        d = super().clean()
        # Hidden, unused fields never block saving: the new model's unless "Add a new model" is chosen, the department name unless
        # "Add a new department" is (the browser keeps whatever was typed in them before the choice changed).
        unused = ([] if self.data.get("device_model") == NEW else list(MODEL_FIELDS)) + ([] if self.data.get("department") == NEW else ["new_department"])
        # Slice 26: likewise the fields of the intake choices not chosen, whose values are dropped too: the service never sees them.
        intake = d.get("intake")
        hidden = [name for choice, names in INTAKE_FIELDS.items() if intake and choice != intake for name in names if name in self.fields]
        for name in unused + hidden:
            self.errors.pop(name, None)
        for name in hidden:
            d[name] = False if name == "take_inspection" else None
        if intake == EXISTING and not d.get("installed_on") and "installed_on" not in self.errors:
            self.add_error("installed_on", "Enter the install date of equipment already in use here.")
        return d

    def model_fields(self) -> dict:
        return {name: self.cleaned_data.get(name) for name in MODEL_FIELDS if name in self.fields}

    def asset_fields(self) -> dict:
        """create_asset's keyword arguments, but for the model and department (which may be new). From the intake choice (slice 26): a
        new device waits for its incoming inspection (added_as new, out of service, no PM dates; due on the day given, else the service's
        default); equipment already in use here is added as existing, in the status and with the PM dates given."""
        d = self.cleaned_data
        fields = {name: d[name] for name in ("tag", "serial", "room", "installed_on", "acquisition_cost", "warranty_end", "condition", "notes")}
        if d["intake"] == EXISTING:
            return {**fields, "added_as": AddedAs.EXISTING, "status": d["status"], "last_pm_on": d["last_pm_on"], "next_pm_on": d["next_pm_on"]}
        return {**fields, "added_as": AddedAs.NEW, "incoming_inspection": INCOMING_WAITING,
                "inspection_due": d.get("inspection_due") if d["intake"] == WAITING else None}

    def intake_options(self) -> list[tuple]:
        """The intake radio's options as the template lays them out: (the radio, its hint, the fields shown under it while chosen)."""
        return [(radio, INTAKE_HINTS[radio.data["value"]], [self[name] for name in INTAKE_FIELDS[radio.data["value"]] if name in self.fields])
                for radio in self["intake"]]


class EditDeviceForm(DeviceForm):
    # The last PM is not on this form (PM work orders set it); the install date is what can satisfy its rule, so its error shows there.
    service_fields = {"last_pm_on": "installed_on"}

    acquisition_cost = _money("Acquisition cost, $", True)
    next_pm_on = _date("Next PM", "Stays as it is when the model changes. Set it here if the new model's PM interval differs.")

    def __init__(self, *args, asset, **kwargs):
        if not kwargs.get("data") and not args:
            kwargs["initial"] = {"serial": asset.serial, "device_model": str(asset.device_model_id), "department": str(asset.department_id),
                                 "room": asset.room, "installed_on": asset.installed_on, "warranty_end": asset.warranty_end,
                                 "acquisition_cost": asset.acquisition_cost, "condition": asset.condition, "next_pm_on": asset.next_pm_on,
                                 "notes": asset.notes}
        super().__init__(*args, **kwargs)
        self.asset = asset
        self.fields["serial"].widget.attrs["autofocus"] = True
        # Slice 26: a device waiting for its incoming inspection has no next PM until the inspection passes (update_asset refuses a
        # date), so the form shows that in words and never sends one.
        self.awaiting = bool(asset.awaiting_inspection)
        if self.awaiting:
            del self.fields["next_pm_on"]
        # Slice 29: a rental, vendor loaner, or demo unit is not ours and its owner maintains it: no install date (it arrived), warranty,
        # acquisition cost (update_asset keeps it 0; Keep it records a price), or next PM, and no notes (a stay carries no free text).
        # Its stay is changed from the drawer's Change details.
        self.temporary = owner_maintains(asset)
        if self.temporary:
            for name in ("installed_on", "warranty_end", "acquisition_cost", "next_pm_on", "notes"):
                self.fields.pop(name, None)

    def changes(self) -> dict:
        """update_asset's keyword arguments: every editable detail on the form (it saves only what differs). Never the tag, status, or
        contract, nor the next PM of a device waiting for its incoming inspection."""
        return {name: self.cleaned_data[name] for name in EDITABLE_FIELDS if name in self.fields}


class UseBeforeForm(forms.Form):
    """Putting a device waiting for its incoming inspection in use before it (slice 26, equipment.services.use_before_inspection): why,
    from UseBeforeInspection's list. Never free text: nobody writes a patient's details into it."""

    reason = forms.ChoiceField(label="Why it goes into use now", choices=UseBeforeInspection.choices, widget=forms.RadioSelect,
                               error_messages={"required": "Choose why the device goes into use before its incoming inspection.",
                                               "invalid_choice": "Choose one of the reasons listed."})

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ub-%s")
        super().__init__(*args, **kwargs)

    def add_service_errors(self, error):
        """The service's refusal: on the reason when it names it, else at the top (the device is no longer waiting, or not out of service)."""
        if hasattr(error, "error_dict"):
            for key, messages in error.message_dict.items():
                self.add_error(key if key in self.fields else None, messages)
        else:
            self.add_error(None, error.messages)
