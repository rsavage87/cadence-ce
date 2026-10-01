"""
Input parsing for Add device and Edit details (slice 12). Model and department choices depend on the tenant, so they are built in
__init__, never at class level. The rules (tags, dates, costs, a new model's fields) live in apps.equipment.services; these forms
parse what was typed and put the services' errors back on the fields they name.
"""
from datetime import date

from django import forms

from apps.equipment.models import AssetStatus, Department, DeviceModel, RiskClass
from apps.equipment.services import EDITABLE_FIELDS

NEW = "new"  # the model or department select's value that reveals the fields for adding one
CONDITIONS = [(1, "1 · Poor"), (2, "2 · Fair"), (3, "3 · Good"), (4, "4 · Very good"), (5, "5 · Excellent")]
STATUSES = [(AssetStatus.IN_SERVICE, "In service"), (AssetStatus.OUT_OF_SERVICE, "Out of service: waiting for incoming inspection")]
# A new model's fields, named as apps.equipment.services.create_device_model takes them (and keys its errors).
MODEL_FIELDS = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "expected_life_years", "list_cost")
NOTES_PLACEHOLDER = "Accessories, mounting, history. No patient information."
# The order web/_asset_fields.html lays the fields out in, so a form sent back with errors focuses the first one on screen.
LAYOUT = ("tag", "serial", "device_model", *MODEL_FIELDS, "department", "room", "new_department", "installed_on", "warranty_end", "acquisition_cost",
          "condition", "last_pm_on", "next_pm_on", "status", "notes")


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
        today = date.today().isoformat()
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
    last_pm_on = _date("Last PM")
    next_pm_on = _date("Next PM", "Blank lets the schedule work it out from the last PM or the install date.")
    status = forms.ChoiceField(label="Status", choices=STATUSES, initial=AssetStatus.IN_SERVICE)
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
    # The new department, used only when the department select says "Add a new department".
    new_department = forms.CharField(label="New department name", required=False, max_length=80)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The category box suggests the facility's categories (a datalist); any new one is allowed.
        self.categories = list(DeviceModel.objects.order_by("category").values_list("category", flat=True).distinct())

    def clean(self):
        d = super().clean()
        # Hidden, unused fields never block saving: the new model's unless "Add a new model" is chosen, the department name unless
        # "Add a new department" is (the browser keeps whatever was typed in them before the choice changed).
        unused = ([] if self.data.get("device_model") == NEW else list(MODEL_FIELDS)) + ([] if self.data.get("department") == NEW else ["new_department"])
        for name in unused:
            self.errors.pop(name, None)
        return d

    def model_fields(self) -> dict:
        return {name: self.cleaned_data.get(name) for name in MODEL_FIELDS}

    def asset_fields(self) -> dict:
        """create_asset's keyword arguments, but for the model and department (which may be new)."""
        d = self.cleaned_data
        return {name: d[name] for name in ("tag", "serial", "room", "installed_on", "acquisition_cost", "warranty_end", "condition", "last_pm_on",
                                           "next_pm_on", "notes", "status")}


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

    def changes(self) -> dict:
        """update_asset's keyword arguments: every editable detail (it saves only what differs). Never the tag, status, or contract."""
        return {name: self.cleaned_data[name] for name in EDITABLE_FIELDS}
