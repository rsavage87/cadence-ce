"""
Input parsing for Log time and Add part in the work order drawer (slice 15). The technician choices depend on the tenant, so they
are built in __init__, never at class level. The rules (hours, dates, rates, quantities, costs, lengths) live in
apps.workorders.costs; these forms parse what was typed and put the service's errors back on the fields they name.
"""
from datetime import date

from django import forms
from django.utils import timezone

from apps.credentials.models import Technician
from apps.workorders import costs

DESCRIPTION_PLACEHOLDER = "What was done (no patient information)"
PART_PLACEHOLDER = "e.g. Door latch (no patient information)"


def _number(label, placeholder="", **attrs):
    # Text, not a number field: apps.workorders.costs says what a valid number is (and refuses a third decimal rather than
    # letting the browser round it). inputmode brings up the number pad on a phone.
    return forms.CharField(label=label, required=False,
                           widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off", "placeholder": placeholder, **attrs}))


money_text = costs.read_money  # the reading of a typed amount, shared with the API (apps/api/views_work.py)


def _text_input(limit: int, **attrs):
    # The browser stops at the limit; anything longer that still arrives gets the service's message (no max_length on the
    # field, so the wording lives in one place).
    return forms.TextInput(attrs={"maxlength": limit, "autocomplete": "off", **attrs})


class CostForm(forms.Form):
    def add_service_errors(self, error):
        """A service's ValidationError on the fields it names; anything else at the top. Never add_error(None, e) with a dict."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)

    def focus_first_error(self):
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so make that the first field with an error."""
        first = next((name for name in self.fields if name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True


class LaborForm(CostForm):
    """Log time. The technician select is only on in-house work orders, and the rate box only for users who may set a rate
    (work-order Approve): everyone else's line is charged at the Settings rate."""

    worked_on = forms.DateField(label="Date worked", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                                error_messages={"invalid": "Enter the date the work was done."})
    hours = _number("Hours", "e.g. 1.5", autofocus=True)
    technician = forms.ChoiceField(label="Technician", required=False, error_messages={"invalid_choice": "Choose a technician from the list."})
    rate = _number("Rate, $ per hour")
    description = forms.CharField(label="Work done, optional", required=False, widget=_text_input(costs.DESCRIPTION_MAX, placeholder=DESCRIPTION_PLACEHOLDER))

    def __init__(self, *args, wo, user, default_rate, can_set_rate: bool, today: date | None = None, **kwargs):
        kwargs.setdefault("auto_id", "wl-%s")
        super().__init__(*args, **kwargs)
        today = today or timezone.localdate()
        last = min(today, wo.completed_on or today)
        self.wo, self.default_rate, self.can_set_rate = wo, default_rate, can_set_rate
        self.fields["worked_on"].widget.attrs.update({"min": wo.opened_on.isoformat(), "max": last.isoformat()})
        if not self.is_bound:
            self.initial.setdefault("worked_on", last)
        if wo.vendor_service:
            del self.fields["technician"]
        else:
            # The facility's active technicians; the default (costs.default_technician) is always one of them.
            self.technicians = {str(t.pk): t for t in Technician.objects.filter(is_active=True)}
            self.fields["technician"].choices = [("", "Choose a technician"), *((pk, t.name) for pk, t in self.technicians.items())]
            default = costs.default_technician(wo, user)
            if default is not None and not self.is_bound:
                self.initial.setdefault("technician", str(default.pk))
        if can_set_rate:
            self.fields["rate"].widget.attrs["placeholder"] = costs.plain(default_rate)
            self.fields["rate"].help_text = f"Blank charges the Settings rate, {costs.money_text(default_rate)} an hour."
        else:
            del self.fields["rate"]

    def clean_rate(self):
        return money_text(self.cleaned_data.get("rate", ""))

    def clean_technician(self):
        value = self.cleaned_data.get("technician", "")
        if not value:
            return None  # the service picks the default, or says to choose one
        tech = self.technicians.get(value)
        if tech is None:
            raise forms.ValidationError("Choose a technician from the list.")
        return tech

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        return {"worked_on": d.get("worked_on"), "hours": d.get("hours", ""), "technician": d.get("technician"),
                "rate": d.get("rate") if self.can_set_rate else None, "description": d.get("description", "")}


class PartForm(CostForm):
    description = forms.CharField(label="Part", required=False, widget=_text_input(costs.DESCRIPTION_MAX, placeholder=PART_PLACEHOLDER, autofocus=True))
    part_number = forms.CharField(label="Part number, optional", required=False, widget=_text_input(costs.PART_NUMBER_MAX, spellcheck="false"))
    quantity = _number("Quantity")
    unit_cost = _number("Unit cost, $", "e.g. 42.00")
    po_number = forms.CharField(label="PO number, optional", required=False, widget=_text_input(costs.PO_NUMBER_MAX, spellcheck="false"))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "wp-%s")
        kwargs.setdefault("initial", {"quantity": "1"})
        super().__init__(*args, **kwargs)

    def clean_unit_cost(self):
        return money_text(self.cleaned_data.get("unit_cost", ""))

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        return {name: d.get(name, "") for name in ("description", "part_number", "quantity", "unit_cost", "po_number")}
