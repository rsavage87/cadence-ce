"""Input parsing for the Contracts screen and the device drawer's support editor. Same rules as forms.py: unknown filter values are dropped."""
from datetime import date
from decimal import Decimal

from django import forms
from django.conf import settings

from apps.contracts.models import Contract, ContractType, Coverage
from apps.contracts.services import STATUS_KEYS, ContractFilters
from apps.pm.dates import add_months

STATUS_CHOICES = [("active", "Active"), ("ending", f"Ending within {settings.CONTRACT_EXPIRY_WARNING_DAYS} days"), ("expired", "Expired")]
ATTACH_COST_SHARE = 0.07  # the modal's suggested annual cost for one device: 7% of acquisition, to the nearest $100


def _one_of(value, allowed) -> str:
    return value if value in allowed else ""


def parse_contract_filters(params) -> ContractFilters:
    return ContractFilters(q=params.get("q", "").strip()[:100], type=_one_of(params.get("type"), ContractType.values),
                           status=_one_of(params.get("status"), STATUS_KEYS))


class ContractForm(forms.Form):
    reference = forms.CharField(max_length=60, widget=forms.TextInput(attrs={"placeholder": "e.g. SC-2026-118", "autofocus": True}))
    vendor = forms.CharField(max_length=120, widget=forms.TextInput(attrs={"placeholder": "Who holds the contract"}))
    type = forms.ChoiceField(choices=ContractType.choices, initial=ContractType.OEM)
    coverage = forms.ChoiceField(choices=Coverage.choices, initial=Coverage.FULL)
    start_on = forms.DateField(label="Start", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    end_on = forms.DateField(label="End", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    annual_cost = forms.DecimalField(label="Annual cost, $", min_value=0, max_digits=12, decimal_places=2, initial=0,
                                     widget=forms.NumberInput(attrs={"min": 0, "step": 100}))
    notes = forms.CharField(required=False, max_length=500, widget=forms.TextInput(attrs={"placeholder": "Renewal terms, response SLA, loaner policy"}))

    def add_service_errors(self, error):
        """Map a service ValidationError (dict or list) back onto the fields so the form re-renders with messages."""
        if hasattr(error, "error_dict"):
            for field, messages in error.message_dict.items():
                self.add_error(field if field in self.fields else None, messages)
        else:
            self.add_error(None, error.messages)


def new_contract_initial(asset=None, today: date | None = None) -> dict:
    today = today or date.today()
    initial = {"start_on": today, "end_on": add_months(today, 12), "annual_cost": 0}
    if asset is not None:
        initial["vendor"] = asset.device_model.manufacturer
        initial["annual_cost"] = Decimal(int(float(asset.acquisition_cost) * ATTACH_COST_SHARE / 100 + 0.5) * 100)
    return initial


def edit_contract_initial(contract) -> dict:
    return {"reference": contract.reference, "vendor": contract.vendor, "type": contract.type, "coverage": contract.coverage,
            "start_on": contract.start_on, "end_on": contract.end_on, "annual_cost": contract.annual_cost, "notes": contract.notes}


def contract_choices(today: date | None = None) -> list[tuple[str, str]]:
    """The support editor's select: in-house first, then every contract by vendor and reference (the mock's supportForm)."""
    today = today or date.today()
    choices = [("", "In-house, no contract")]
    for c in Contract.objects.order_by("vendor", "reference"):
        choices.append((str(c.id), f"{c.reference} · {c.vendor} · {c.get_coverage_display()}{' (expired)' if c.end_on < today else ''}"))
    return choices
