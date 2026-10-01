"""
Input parsing for the device model catalog (slice 14): Add model, Edit details, and the risk score. The rules (required text, a
name unique in any letter case, interval and life ranges, the rubric's ranges and bands) live in apps.equipment.services; these
forms parse what was typed and put the services' errors back on the fields they name. The facility's categories are read in
__init__, never at class level.
"""
from django import forms

from apps.equipment.models import DeviceModel, RiskClass
from apps.equipment.services import RISK_PARTS, risk_band

# The fields Add model sends to create_device_model, in the order the form lays them out (Edit details has all but risk_class).
MODEL_FIELDS = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "expected_life_years", "list_cost")
EDIT_FIELDS = tuple(f for f in MODEL_FIELDS if f != "risk_class")

# What each score means, short (the Settings rubric's parts; the classic clinical engineering risk scale).
RISK_MEANINGS = {
    "function": {10: "Life support", 9: "Surgical and intensive care", 8: "Physical therapy and treatment", 7: "Surgical and intensive care monitoring",
                 6: "Other physiological monitoring and diagnosis", 5: "Analytical laboratory", 4: "Laboratory accessories", 3: "Computers and related",
                 2: "Patient related, other", 1: "No patient contact"},
    "physical": {5: "Failure could cause death", 4: "Patient or operator injury", 3: "Wrong therapy or misdiagnosis", 2: "Equipment damage",
                 1: "No significant risk"},
    "maintenance": {5: "Extensive: calibration and parts replacement", 4: "Above average", 3: "Average: performance and safety checks",
                    2: "Below average", 1: "Minimal: visual inspection"},
    "incidents": {2: "Repeated incidents or recalls", 1: "Some incidents or recalls", 0: "None of note"},
}


class ServiceErrorsMixin:
    """Show a service's ValidationError on the fields it names; anything the form has no field for goes at the top. Sent back with
    errors, the first field with one gets [autofocus] (htmx focuses it on the swap)."""

    layout: tuple = ()

    def add_service_errors(self, error):
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)

    def focus_first_error(self):
        first = next((name for name in self.layout if name in self.fields and name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True


class DeviceModelForm(ServiceErrorsMixin, forms.Form):
    """Add model (with a risk class) and Edit details (without: a scored model's class follows its score, and changing it is the
    risk score's job). Not required here: the services say what each field needs."""

    layout = MODEL_FIELDS

    manufacturer = forms.CharField(label="Manufacturer", required=False, max_length=120,
                                   widget=forms.TextInput(attrs={"autofocus": True, "required": True, "autocomplete": "off"}))
    model = forms.CharField(label="Model name or number", required=False, max_length=120,
                            widget=forms.TextInput(attrs={"required": True, "autocomplete": "off"}))
    description = forms.CharField(label="Description", required=False, max_length=200,
                                  widget=forms.TextInput(attrs={"required": True, "placeholder": "Plain-language name, e.g. Infusion pump"}))
    category = forms.CharField(label="Category", required=False, max_length=80,
                               widget=forms.TextInput(attrs={"list": "dm-categories", "required": True, "placeholder": "e.g. Infusion pumps",
                                                             "autocomplete": "off"}))
    risk_class = forms.ChoiceField(label="Risk class", required=False, choices=RiskClass.choices, initial=RiskClass.MEDIUM,
                                   help_text="A starting class. Scoring the model with the rubric sets it from then on.")
    oem_pm_interval_months = forms.IntegerField(label="OEM PM interval, months", required=False, initial=12,
                                                widget=forms.NumberInput(attrs={"min": 1, "max": 120, "required": True}))
    expected_life_years = forms.IntegerField(label="Expected life, years", required=False, initial=8, widget=forms.NumberInput(attrs={"min": 1, "max": 50}))
    list_cost = forms.DecimalField(label="List cost, $", required=False, max_digits=12, decimal_places=2, help_text="Blank is 0.",
                                   widget=forms.NumberInput(attrs={"min": 0, "step": "0.01"}))

    def __init__(self, *args, device_model: DeviceModel | None = None, **kwargs):
        kwargs.setdefault("auto_id", "dm-%s")
        if device_model is not None and not args and not kwargs.get("data"):
            kwargs["initial"] = {f: getattr(device_model, f) for f in EDIT_FIELDS}
        super().__init__(*args, **kwargs)
        self.device_model = device_model
        if device_model is not None:
            del self.fields["risk_class"]
            self.fields["oem_pm_interval_months"].help_text = "A new interval applies from each device's next PM; no date moves now."
        # The category box suggests the facility's categories (a datalist); any new one is allowed.
        self.categories = list(DeviceModel.objects.order_by("category").values_list("category", flat=True).distinct())

    def service_fields(self) -> dict:
        """create_device_model's or update_device_model's keyword arguments (update saves only what differs)."""
        return {name: self.cleaned_data.get(name) for name in (MODEL_FIELDS if self.device_model is None else EDIT_FIELDS)}


def _choices(key: str, low: int, high: int) -> list[tuple[str, str]]:
    return [("", "Choose"), *((str(n), f"{n} · {RISK_MEANINGS[key][n]}") for n in range(high, low - 1, -1))]


class RiskScoreForm(ServiceErrorsMixin, forms.Form):
    """The rubric's four parts as selects, highest first, each value with what it means. set_risk_score checks the ranges."""

    layout = tuple(key for key, *_rest in RISK_PARTS)

    def __init__(self, *args, device_model: DeviceModel, **kwargs):
        kwargs.setdefault("auto_id", "rk-%s")
        if not args and not kwargs.get("data") and device_model.risk_score is not None:
            kwargs["initial"] = {key: str(getattr(device_model, field)) for key, field, *_rest in RISK_PARTS}
        super().__init__(*args, **kwargs)
        self.device_model = device_model
        for key, _field, label, low, high in RISK_PARTS:
            self.fields[key] = forms.ChoiceField(label=f"{label}, {low} to {high}", required=False, choices=_choices(key, low, high))
        self.fields["function"].widget.attrs["autofocus"] = True

    def parts(self) -> dict:
        """set_risk_score's keyword arguments, as chosen (blank is None, which it refuses with the range)."""
        return {key: (self.cleaned_data.get(key) or None) for key, *_rest in RISK_PARTS}

    def preview(self) -> dict | None:
        """The score and band of what is chosen so far, for the modal's running total; None until all four are chosen."""
        values = []
        for key, _field, _label, low, high in RISK_PARTS:
            raw = self.data.get(key, "") if self.is_bound else self.initial.get(key, "")
            # isdecimal, not isdigit: int() refuses some digits ("²") that isdigit accepts
            if not (isinstance(raw, str) and raw.isdecimal() and low <= int(raw) <= high):
                return None
            values.append(int(raw))
        score = sum(values)
        return {"score": score, "risk": risk_band(score), "label": RiskClass(risk_band(score)).label}
