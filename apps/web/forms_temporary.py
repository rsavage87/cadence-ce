"""
Input parsing for temporary equipment (slice 29): Add rental or loaner, a stay's Change details, Return to owner, and Keep it. The rules
live in apps.equipment.services (add_temporary_device, update_temporary, return_to_owner, keep_temporary_device); these forms parse
what was typed and put the services' errors back on the fields they name. Choices that depend on the facility are built in __init__.

Add rental or loaner (TemporaryDeviceForm) takes Add device's tag, model, and department fields (forms_equipment.NewDeviceForm, with its
"Add a new model" and "Add a new department"), its serial and room, and its way of giving out the incoming inspection (an inspector, or
"Assign the inspection to me", by views_equipment.intake_offer); none of the details of a device of ours (install date, warranty, cost,
condition, PM dates, notes: no free text on a stay). Its own: whose it is (rental, vendor loaner, demo unit), the owner (a company, with
the names the facility already knows offered), the agreement, PO, or RMA number (a token: never a patient's name or record number), when
it arrived, when it is due back, the owner's PM date from its sticker, the device of ours a vendor loaner stands in for (by its tag), and
how it arrives: new and waiting for its incoming inspection, new and inspected now, or already on site when entered. Hidden choices'
values are dropped, as Add device's are.
"""
from datetime import timedelta

from django import forms
from django.utils import timezone

from apps.contracts.models import Contract
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership, ReturnCleaning, ReturnData
from apps.equipment.services import INCOMING_WAITING, OWNED, OWNER_MAX, REFERENCE_MAX, TEMPORARY_KINDS
from apps.workorders.inspections import INSPECTION_DUE_DAYS

from .forms_equipment import EXISTING, INSPECT_NOW, MODEL_FIELDS, NEW, WAITING, NewDeviceForm, _date, _money

KIND_CHOICES = [(k, Ownership(k).label) for k in TEMPORARY_KINDS]
KIND_HINTS = {
    Ownership.RENTAL: "Rented from a company for a time: a specialty bed, pumps for a surge.",
    Ownership.LOANER: "Lent by a vendor while a device of ours is out for repair.",
    Ownership.DEMO: "On trial or evaluation before a purchase.",
}
INTAKE_CHOICES = [(WAITING, "New here: waiting for its incoming inspection"), (INSPECT_NOW, "New here: inspect it now"),
                  (EXISTING, "Already on site (entered after it arrived)")]
INTAKE_HINTS = {
    WAITING: "Out of service until it passes the rental checklist: the incoming checklist and the owner's PM label. No PM of ours: its "
             "owner maintains it.",
    INSPECT_NOW: "Added waiting, with the inspection assigned to you: record it next, with its checklist.",
    EXISTING: "In service, with no incoming inspection: enter the day it arrived.",
}
INTAKE_FIELDS = {WAITING: ("inspection_due", "take_inspection", "inspector"), INSPECT_NOW: (), EXISTING: ()}
# Add device's fields a rental, vendor loaner, or demo unit never takes: it is not ours (no cost, warranty, or install date; arrived is
# its day), its owner maintains it (no PM dates), and a stay carries no free text.
DROPPED = ("installed_on", "warranty_end", "acquisition_cost", "condition", "notes", "status", "last_pm_on", "next_pm_on")
REFERENCE_LABEL = "Rental agreement, PO, or RMA number"
REFERENCE_HELP = "As the owner's paperwork shows it, without spaces. Never a patient's name or record number."
STANDS_IN_HELP = "The asset tag of our device that is out for repair."
# The order web/_temporary_new.html lays the fields out in, so a form sent back with errors focuses the first one on screen.
LAYOUT = ("kind", "tag", "serial", "device_model", *MODEL_FIELDS, "department", "room", "new_department", "owner", "owner_reference",
          "stands_in_for", "arrived_on", "due_back_on", "owner_pm_due_on", "intake", "inspection_due", "take_inspection", "inspector")


def _tag_field(label, help_text, required=False):
    return forms.CharField(label=label, required=required, max_length=40, help_text=help_text,
                           widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false", "placeholder": "e.g. CE-10241"}))


def _owner_field():
    return forms.CharField(label="Owner", required=False, max_length=OWNER_MAX,
                           widget=forms.TextInput(attrs={"list": "tmp-owners", "autocomplete": "off", "placeholder": "The company that owns it"}))


def _reference_field():
    return forms.CharField(label=REFERENCE_LABEL, required=False, max_length=REFERENCE_MAX, help_text=REFERENCE_HELP,
                           widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false", "placeholder": "e.g. RA-2026-0042"}))


def known_owners() -> list[str]:
    """The names the owner box suggests: owners of earlier temporary devices, contract vendors, and the catalog's manufacturers (a
    vendor loaner is usually the manufacturer's). Three queries, distinct and sorted."""
    names = set(Asset.objects.exclude(OWNED).exclude(owner="").values_list("owner", flat=True).distinct())
    names |= set(Contract.objects.values_list("vendor", flat=True).distinct())
    names |= set(DeviceModel.objects.values_list("manufacturer", flat=True).distinct())
    return sorted((n for n in names if n), key=str.lower)


def device_by_tag(tag: str):
    """The facility's device with this tag (any letter case), or None. The services decide whether it may be stood in for."""
    tag = (tag or "").strip()
    return Asset.objects.filter(tag__iexact=tag).first() if tag else None


def _stands_in_for(value):
    """A stands-in-for tag typed: None for blank, else the device, refused when no device here has it."""
    if not value:
        return None
    device = device_by_tag(value)
    if device is None:
        raise forms.ValidationError(f"No device here has the tag {value}.")
    return device


class ServiceErrors:
    """A service's ValidationError on the fields it names (through service_fields where this form calls them something else); anything
    the form has no field for goes at the top."""

    service_fields: dict[str, str] = {}

    def add_service_errors(self, error):
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            field = self.service_fields.get(key, key)
            self.add_error(field if field in self.fields else None, messages)


class TemporaryDeviceForm(NewDeviceForm):
    """Add rental or loaner (equipment.services.add_temporary_device). See the module's docstring."""

    kind = forms.ChoiceField(label="Whose is it", choices=KIND_CHOICES, initial=Ownership.RENTAL, widget=forms.RadioSelect,
                             error_messages={"required": "Choose rental, vendor loaner, or demo unit.",
                                             "invalid_choice": "Choose rental, vendor loaner, or demo unit."})
    owner = _owner_field()
    owner_reference = _reference_field()
    arrived_on = _date("Arrived", "Today unless it came earlier.")
    due_back_on = _date("Due back", "When the agreement says it goes back. Blank if open-ended.")
    owner_pm_due_on = _date("Owner's PM due", "From the owner's PM sticker on the unit.")
    stands_in_for = _tag_field("Stands in for", STANDS_IN_HELP)
    intake = forms.ChoiceField(label="How it arrives", choices=INTAKE_CHOICES, initial=WAITING, widget=forms.RadioSelect,
                               error_messages={"required": "Say how it arrives.", "invalid_choice": "Choose one of the ways listed."})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in DROPPED:
            del self.fields[name]
        self.fields["intake"].choices = [c for c in INTAKE_CHOICES if c[0] != INSPECT_NOW or self.offer.inspect_now]
        self.fields["serial"].widget.attrs["required"] = True  # the service requires it too: the unit's own number
        self.fields["serial"].help_text = "On the unit itself: a unit that was here before is found by it."
        self.fields["tag"].help_text = "The CE sticker you put on it while it is here."
        self.fields["tag"].widget.attrs["placeholder"] = "e.g. T-0042"
        self.fields["tag"].widget.attrs.pop("autofocus", None)
        self.fields["kind"].widget.attrs["autofocus"] = True
        today = timezone.localdate()
        self.fields["arrived_on"].initial = today
        self.fields["arrived_on"].widget.attrs["max"] = today.isoformat()
        self.fields["inspection_due"].initial = today + timedelta(days=INSPECTION_DUE_DAYS)
        self.owners = known_owners()

    def clean_stands_in_for(self):
        return _stands_in_for(self.cleaned_data.get("stands_in_for", "").strip())

    def clean(self):
        # Not NewDeviceForm.clean: its rules name fields this form does not have (the install date of existing equipment).
        d = forms.Form.clean(self)
        unused = ([] if self.data.get("device_model") == NEW else list(MODEL_FIELDS)) + ([] if self.data.get("department") == NEW else ["new_department"])
        intake = d.get("intake")
        hidden = [name for choice, names in INTAKE_FIELDS.items() if intake and choice != intake for name in names if name in self.fields]
        if d.get("kind") != Ownership.LOANER:
            hidden.append("stands_in_for")  # shown for a vendor loaner only
        for name in [n for n in unused + hidden if n in self.fields]:
            self.errors.pop(name, None)
        for name in hidden:
            d[name] = False if name == "take_inspection" else None
        return d

    def asset_fields(self) -> dict:
        """add_temporary_device's keyword arguments, but for the model and department (which may be new). From the intake choice:
        new and waiting ("waiting", due on the day given or the service's default), new and inspected now ("": it still waits, and the
        view assigns the inspection and opens its Mark completed), or already on site (added_as existing: no inspection)."""
        d = self.cleaned_data
        fields = {name: d.get(name) for name in ("tag", "serial", "room", "kind", "owner", "owner_reference", "arrived_on", "due_back_on",
                                                 "owner_pm_due_on", "stands_in_for")}
        if d["intake"] == EXISTING:
            return {**fields, "added_as": AddedAs.EXISTING}
        if d["intake"] == WAITING:
            return {**fields, "added_as": AddedAs.NEW, "incoming_inspection": INCOMING_WAITING, "inspection_due": d.get("inspection_due")}
        return {**fields, "added_as": AddedAs.NEW, "incoming_inspection": ""}

    def focus_first_error(self):
        first = next((name for name in LAYOUT if name in self.fields and name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True

    def kind_options(self) -> list[tuple]:
        """The Whose radio's options: (the radio, its hint)."""
        return [(radio, KIND_HINTS.get(radio.data["value"], "")) for radio in self["kind"]]

    def intake_options(self) -> list[tuple]:
        """The intake radio's options as the template lays them out: (the radio, its hint, the fields shown under it while chosen)."""
        return [(radio, INTAKE_HINTS[radio.data["value"]], [self[name] for name in INTAKE_FIELDS[radio.data["value"]] if name in self.fields])
                for radio in self["intake"]]


class StayForm(ServiceErrors, forms.Form):
    """Change details of a stay (equipment.services.update_temporary): the owner, the reference, due back, the owner's PM date, and for
    a vendor loaner the device of ours it stands in for. A blank date or stands-in tag clears it."""

    owner = _owner_field()
    owner_reference = _reference_field()
    due_back_on = _date("Due back", "Blank if open-ended.")
    owner_pm_due_on = _date("Owner's PM due", "From the owner's PM sticker: record the new date when the owner has done it.")
    stands_in_for = _tag_field("Stands in for", STANDS_IN_HELP)

    def __init__(self, *args, asset, **kwargs):
        kwargs.setdefault("auto_id", "st-%s")
        if not args and not kwargs.get("data"):
            kwargs["initial"] = {"owner": asset.owner, "owner_reference": asset.owner_reference, "due_back_on": asset.due_back_on,
                                 "owner_pm_due_on": asset.owner_pm_due_on,
                                 "stands_in_for": asset.stands_in_for.tag if asset.stands_in_for_id else ""}
        super().__init__(*args, **kwargs)
        self.asset = asset
        self.fields["owner"].widget.attrs["autofocus"] = True
        if asset.arrived_on:
            self.fields["due_back_on"].widget.attrs["min"] = asset.arrived_on.isoformat()  # the service refuses an earlier day
        if asset.ownership != Ownership.LOANER:
            del self.fields["stands_in_for"]
        self.owners = known_owners()

    def clean_stands_in_for(self):
        return _stands_in_for(self.cleaned_data.get("stands_in_for", "").strip())

    def changes(self) -> dict:
        """update_temporary's keyword arguments: every value on the form (the service saves only what differs)."""
        return {name: self.cleaned_data[name] for name in ("owner", "owner_reference", "due_back_on", "owner_pm_due_on", "stands_in_for")
                if name in self.fields}


class ReturnForm(ServiceErrors, forms.Form):
    """Return to owner (equipment.services.return_to_owner): the day it went back, how it was cleaned (OSHA 1910.1030), and what was
    done about patient data on it (HIPAA 164.310(d)(2)(ii)). Choices only."""

    on = _date("Returned on")
    cleaning = forms.ChoiceField(label="How it was cleaned before it left", choices=ReturnCleaning.choices, widget=forms.RadioSelect,
                                 error_messages={"required": "Say how it was cleaned before it left.",
                                                 "invalid_choice": "Choose one of the ways listed."})
    data = forms.ChoiceField(label="Patient data on it", choices=ReturnData.choices, widget=forms.RadioSelect,
                             error_messages={"required": "Say what was done about patient data on it.",
                                             "invalid_choice": "Choose one of the answers listed."})

    def __init__(self, *args, asset, **kwargs):
        kwargs.setdefault("auto_id", "rt-%s")
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fields["on"].initial = today
        self.fields["on"].widget.attrs["max"] = today.isoformat()
        if asset.arrived_on:
            self.fields["on"].widget.attrs["min"] = asset.arrived_on.isoformat()
        # Review fix: a missing unit leaves only as not in hand (lost, settled with its owner); one in hand never as that
        missing = asset.status == AssetStatus.MISSING
        self.fields["cleaning"].choices = [(v, label) for v, label in ReturnCleaning.choices if (v == ReturnCleaning.NOT_IN_HAND) == missing]
        if missing:
            self.fields["cleaning"].initial = ReturnCleaning.NOT_IN_HAND


class KeepForm(ServiceErrors, forms.Form):
    """Keep it (equipment.services.keep_temporary_device): what the facility paid, its first PM as ours (today by default: the
    acceptance PM), and the warranty."""

    acquisition_cost = _money("What the facility paid, $", False, "0 if nothing (a demo unit left free).")
    next_pm_on = _date("First PM as ours", "Today unless you set it: the facility's acceptance PM.")
    warranty_end = _date("Warranty ends")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "kp-%s")
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fields["next_pm_on"].initial = today
        self.fields["next_pm_on"].widget.attrs["min"] = today.isoformat()
        self.fields["acquisition_cost"].widget.attrs["autofocus"] = True
