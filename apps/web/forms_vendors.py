"""
Input parsing for the Contracts screen's Vendor staff tab (slice 31): its filters, and the modals that add and change vendor engineers,
their training, and the vendors' attestations. The rules (and their words) are apps.vendors.services' and apps.vendors.rules'; these
forms read what was typed and put the services' refusals back on the fields they name. Every field is optional here so that a missing
value is refused in the service's words; a form refuses only what it cannot read (a date that is not one, a choice it never offered).
Choices that depend on the facility (its catalog, a vendor's engineers, the vendor names it knows) are built in __init__, never at
class level. A value a hidden choice holds is dropped, as Add device's are: the product category while device models are chosen, the
models while a category is, and the certificate number once more than one engineer is ticked (a certificate is one engineer's).

Privacy: an engineer is a name and a vendor; nothing here asks for anything else (badges, immunizations, and background checks belong
to the facility's vendor credentialing system).
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django import forms

from apps.equipment.models import DeviceModel
from apps.pm.dates import add_months
from apps.vendors import rules
from apps.vendors.models import Covers, TrainingSource
from apps.vendors.rules import NAME_MAX
from apps.vendors.services import CERTIFICATE_MAX, COMPANY_MAX, REFERENCE_MAX

from .forms_temporary import ServiceErrors

LINE_SEP = "|"  # a product category's value: "<manufacturer>|<category>", as the catalog writes them
VENDOR_NAME_ROWS = 3  # Add attestation's vendor name boxes: the contract's name, its field service, one more
DATE_FORMAT = "%Y-%m-%d"
VENDOR_LIST = "vs-vendors"  # the datalist of the vendor names the facility knows (accounts.services.company_suggestions)
ATTESTATION_STATES = ("A vendor's written statement that everyone who services these devices for us, on site, at its depot, or remotely, "
                      "is trained on them, and that their training records are available on request: a yearly letter, or a clause "
                      "of a contract.")
CREDENTIALING_ELSEWHERE = ("Badges, immunizations, and background checks belong to the facility's vendor credentialing system, not "
                           "here.")


@dataclass(frozen=True)
class VendorFilters:
    q: str = ""  # a vendor's name (any spelling) or an engineer's
    vendor: str = ""  # one vendor, by apps.vendors.rules.vendor_key (the survey binder's gaps and the contract drawer link here)


def parse_vendor_filters(params) -> VendorFilters:
    return VendorFilters(q=params.get("q", "").strip()[:100], vendor=" ".join(params.get("vendor", "").split())[:COMPANY_MAX])


def _text(label, *, max_length, placeholder="", help_text="", **attrs):
    attrs = {"autocomplete": "off", "maxlength": max_length, **({"placeholder": placeholder} if placeholder else {}), **attrs}
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.TextInput(attrs=attrs))


def _day(label, *, help_text=""):
    return forms.DateField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs={"type": "date"}, format=DATE_FORMAT),
                           error_messages={"invalid": "Enter a date."})


# --- the catalog -----------------------------------------------------------------------------------------------------------------

def _maker_first(groups: dict, vendor: str) -> list:
    """The catalog's groups by manufacturer, the vendor's own make first (the maker its name is, field service or not: rules.vendor_key)."""
    key = rules.vendor_key(vendor)
    return sorted(groups.items(), key=lambda g: not (key and rules.vendor_key(g[0]) == key))


def model_choices(vendor: str = "") -> list:
    """The catalog's device models grouped by manufacturer, for a multi-select: [(manufacturer, [(id, "model · category"), ...])]."""
    groups: dict[str, list] = {}
    for pk, maker, model, category in DeviceModel.objects.order_by("manufacturer", "model").values_list("pk", "manufacturer", "model", "category"):
        groups.setdefault(maker, []).append((str(pk), f"{model} · {category}"))
    return _maker_first(groups, vendor)


def line_choices(vendor: str = "") -> list:
    """One manufacturer's product category, as the catalog writes them, grouped by manufacturer (a category alone would cover another
    maker's devices: never offered)."""
    groups: dict[str, list] = {}
    rows = DeviceModel.objects.order_by("manufacturer", "category").values_list("manufacturer", "category").distinct()
    for maker, category in rows:
        groups.setdefault(maker, []).append((f"{maker}{LINE_SEP}{category}", f"{maker} · {category}"))
    return [("", "Choose a product category"), *_maker_first(groups, vendor)]


class CoverageFields(ServiceErrors, forms.Form):
    """What a record covers: device models (several, from the catalog) or one manufacturer's product category; an attestation may
    also cover every device the vendor services for us (`allow_all`)."""

    covers = forms.ChoiceField(label="Covers", required=False, widget=forms.RadioSelect,
                               error_messages={"invalid_choice": "Choose what it covers."})
    model_ids = forms.MultipleChoiceField(label="Device models", required=False,
                                          widget=forms.SelectMultiple(attrs={"size": 8}),
                                          error_messages={"invalid_choice": "Choose device models from the catalog."})
    line = forms.ChoiceField(label="Product category", required=False,
                             error_messages={"invalid_choice": "Choose a product category from the catalog."})
    service_fields = {"manufacturer": "line", "category": "line"}
    allow_all = False

    def __init__(self, *args, vendor: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        kinds = [Covers.ALL, Covers.MODELS, Covers.LINE] if self.allow_all else [Covers.MODELS, Covers.LINE]
        self.fields["covers"].choices = [(k, Covers(k).label) for k in kinds]
        self.fields["covers"].initial = kinds[0]
        self.fields["model_ids"].choices = model_choices(vendor)
        self.fields["line"].choices = line_choices(vendor)

    def clean(self):
        """The picker the covers choice hides is dropped, its refusals too (a stale choice left in it is not this record's)."""
        d = super().clean()
        hidden = {Covers.MODELS: ("line",), Covers.LINE: ("model_ids",), Covers.ALL: ("model_ids", "line")}.get(d.get("covers"), ())
        for name in hidden:
            self.errors.pop(name, None)
            d.pop(name, None)
        return d

    def add_service_errors(self, error):
        """A category left unchosen is one refusal ("Choose a product category"), not the service's two (its maker, its category)."""
        if hasattr(error, "error_dict") and "manufacturer" in error.message_dict:
            errors = {k: v for k, v in error.message_dict.items() if k not in ("manufacturer", "category")}
            error = forms.ValidationError({**errors, "line": "Choose a product category."})
        super().add_service_errors(error)

    def coverage_kwargs(self) -> dict:
        """The services' coverage arguments; the one the covers choice hides is left out (dropped, as a hidden field's value is)."""
        d = self.cleaned_data
        covers = d.get("covers") or ""
        maker, _, category = (d.get("line") or "").partition(LINE_SEP) if covers == Covers.LINE else ("", "", "")
        return {"covers": covers, "model_ids": list(d.get("model_ids") or []) if covers == Covers.MODELS else [],
                "manufacturer": maker, "category": category}


# --- engineers ---------------------------------------------------------------------------------------------------------------------

class EngineerForm(ServiceErrors, forms.Form):
    """Add engineer (vendors.services.add_engineer) and Change engineer (update_engineer, with `is_active`). `fixed`: the engineer is
    named on work orders, so their vendor stays (the box is read-only; the service refuses a change anyway)."""

    name = _text("Name", max_length=NAME_MAX, placeholder="As the vendor's service reports name them", autofocus=True)
    company = _text("Vendor", max_length=COMPANY_MAX, placeholder="As your work orders and contracts name it", list=VENDOR_LIST,
                    help_text="Pick a name your work orders and contracts use, or type one.")
    is_active = forms.BooleanField(label="Active: offered when work is completed", required=False)

    def __init__(self, *args, change: bool = False, fixed: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.change = change
        if not change:
            del self.fields["is_active"]
        if fixed:
            self.fields["company"].widget.attrs["readonly"] = True
            self.fields["company"].help_text = ("Named on work orders, so the vendor stays. An engineer who moved to another vendor is "
                                                "added again under that vendor.")

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        out = {"name": d.get("name", ""), "company": d.get("company", "")}
        if self.change:
            out["is_active"] = bool(d.get("is_active"))
        return out


def engineer_initial(engineer) -> dict:
    return {"name": engineer.name, "company": engineer.company, "is_active": engineer.is_active}


# --- trainings -------------------------------------------------------------------------------------------------------------------

class TrainingForm(CoverageFields):
    """Add training (vendors.services.add_training): one coverage, source, and dates for one or several of a vendor's engineers (a
    class the vendor ran), the certificate number for one engineer only. `engineers` are the ones offered: the vendor's active
    engineers, and the engineer the modal was opened from."""

    engineers = forms.MultipleChoiceField(label="Engineers", required=False, widget=forms.CheckboxSelectMultiple,
                                          error_messages={"invalid_choice": "Choose engineers on file."})
    source = forms.ChoiceField(label="Source", required=False, choices=TrainingSource.choices,
                               error_messages={"invalid_choice": "Choose where the training came from."})
    certificate = _text("Certificate number, if any", max_length=CERTIFICATE_MAX, placeholder="e.g. PH-MX-118204", spellcheck="false",
                        help_text="As it is printed, without spaces. One engineer's: tick one engineer to enter it.")
    issued_on = _day("Issued")
    expires_on = _day("Expires, if it does", help_text="Blank: the certificate states no expiry.")

    def __init__(self, *args, engineers=(), today: date, **kwargs):
        super().__init__(*args, **kwargs)
        self.offered = {str(e.pk): e for e in engineers}
        self.fields["engineers"].choices = [(pk, e.name + ("" if e.is_active else " (inactive)")) for pk, e in self.offered.items()]
        self.fields["source"].initial = TrainingSource.OEM
        self.fields["issued_on"].widget.attrs["max"] = today.isoformat()
        self.fields["issued_on"].initial = today
        if len(self.offered) == 1 and not self.is_bound:
            self.fields["engineers"].initial = list(self.offered)

    def chosen(self) -> list:
        return [self.offered[pk] for pk in self.cleaned_data.get("engineers") or [] if pk in self.offered]

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        certificate = (d.get("certificate") or "").strip() if len(d.get("engineers") or []) <= 1 else ""  # hidden for several
        return {**self.coverage_kwargs(), "source": d.get("source") or "", "certificate": certificate, "issued_on": d.get("issued_on"),
                "expires_on": d.get("expires_on")}


# --- attestations ----------------------------------------------------------------------------------------------------------------

class _Names(forms.Widget):
    """Several boxes of one name (the template draws them): every value sent."""

    def value_from_datadict(self, data, files, name):
        return data.getlist(name) if hasattr(data, "getlist") else list(data.get(name) or [])


class NamesField(forms.Field):
    widget = _Names

    def to_python(self, value):
        return [" ".join(str(v).split()) for v in (value or []) if str(v).strip()]


class AttestationForm(CoverageFields):
    """Add attestation (vendors.services.add_attestation): the vendor's names (one or more, as the facility's work orders and contracts
    write them), what it covers, its reference, the day it was received, and the period it states. Valid from left blank is the day it
    was received; valid through left blank is a year after valid from, less a day (a yearly letter)."""

    vendor_names = NamesField(label="Vendor names", required=False)
    reference = _text("Reference", max_length=REFERENCE_MAX, placeholder="e.g. LTR-2026-01 or SC-2026-118/7.2", spellcheck="false",
                      help_text="The letter's reference, or the contract and clause, without spaces.")
    received_on = _day("Received")
    valid_from = _day("Valid from", help_text="Blank: the day it was received.")
    valid_through = _day("Valid through", help_text="Blank: a year after valid from, less a day.")
    allow_all = True

    def __init__(self, *args, today: date, vendor: str = "", **kwargs):
        data = args[0] if args else kwargs.get("data")
        typed = [n for n in (data.getlist("vendor_names") if hasattr(data, "getlist") else []) if n.strip()]
        super().__init__(*args, vendor=typed[0] if typed else vendor, **kwargs)
        self.fields["received_on"].widget.attrs["max"] = today.isoformat()
        self.fields["received_on"].initial = today
        self.first_vendor = vendor
        self.unasked: set[str] = set()  # dates left blank whose default comes from a date not given either: refused through it

    def names_shown(self) -> list[str]:
        """The vendor name boxes' values: what was typed (a refusal keeps it), else the vendor the modal was opened for."""
        names = list(self.data.getlist("vendor_names")) if self.is_bound and hasattr(self.data, "getlist") else (
            [self.first_vendor] if self.first_vendor else [])
        return (names + [""] * VENDOR_NAME_ROWS)[:max(VENDOR_NAME_ROWS, len(names))]

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        received, valid_from, valid_through = d.get("received_on"), d.get("valid_from"), d.get("valid_through")
        if valid_from is None:
            valid_from = received
            if received is None:
                self.unasked.add("valid_from")
        if valid_through is None:
            if valid_from is not None:
                try:
                    valid_through = add_months(valid_from, 12) - timedelta(days=1)
                except (ValueError, OverflowError):  # review fix: a year past 9999: left blank, the service asks for it in words
                    pass
            else:
                self.unasked.add("valid_through")
        return {**self.coverage_kwargs(), "vendor_names": d.get("vendor_names") or [], "reference": d.get("reference", ""),
                "received_on": received, "valid_from": valid_from, "valid_through": valid_through}

    def add_service_errors(self, error):
        """A date left blank whose default comes from a date not given either is refused through that one ("Enter the day it was
        received"), never as a second refusal of its own."""
        if hasattr(error, "error_dict"):
            kept = {k: v for k, v in error.message_dict.items() if k not in self.unasked}
            if kept:
                error = forms.ValidationError(kept)
        super().add_service_errors(error)
