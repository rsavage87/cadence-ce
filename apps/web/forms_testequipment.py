"""
Input parsing for the Equipment screen's Test equipment tab (slice 35): the list's filters, and the forms of Add test equipment (with its
latest certificate, optional), Change, Record calibration, and the tolerance review's decision. The rules live in
apps.testequipment.services: these forms render the fields, keep what was typed, and hand the text to the services as typed (dates as
ISO text, choices as their slugs), so every problem comes back at once in the services' words, keyed by the field it names. Nothing here
is free text beyond a make, a model, a serial, and a provider's name (no notes on test equipment: models.py).
"""
from dataclasses import dataclass
from datetime import date

from django import forms
from django.urls import reverse

from apps.credentials.models import Technician
from apps.testequipment import rules
from apps.testequipment.models import (
    INTERVAL_DEFAULT,
    INTERVAL_MAX,
    INTERVAL_MIN,
    SERIAL_MAX,
    TAG_MAX,
    TEXT_MAX,
    AsFound,
    Basis,
    Kind,
    Mark,
    Program,
    Result,
    Status,
)
from apps.testequipment.services import default_due, due_cap
from apps.vendors.rules import fmt

from .forms import parse_uuid

DATE = {"type": "date"}
CHOOSE = ("", "Choose…")
NOBODY = "none"  # the held-by filter: items nobody holds
# The list's status filter: every status but retired (the default: a retired item is out of the program), one status, or all.
STATUS_FILTERS = [("", "Not retired"), *Status.choices, ("all", "All, retired too")]
STANDING_FILTERS = [("", "Any standing"), *rules.FILTERS.items()]
MARK_WORDS = {Mark.SHOWN: "Certificate carries an ISO/IEC 17025 accreditation mark", Mark.NOT_SHOWN: "No accreditation mark on the certificate"}
PROGRAM_CHOICES = [(Program.CALIBRATION, "Calibration: its certificates calibrate it against a reference"),
                   (Program.VERIFICATION, "Verification: its certificates verify it (a simulator with no primary reference, such as a SpO2 "
                                          "simulator)")]
CALIBRATION_FIELDS = ("calibrated_on", "due_on", "provider", "certificate", "accreditation", "as_found", "adjusted", "result")
ITEM_FIELDS = ("tag", "kind", "manufacturer", "model", "serial", "program", "interval_months", "held_by")
# The order the modals lay their fields out in (a form sent back with errors focuses the first one on screen)
ITEM_LAYOUT = ("tag", "kind", "manufacturer", "model", "serial", "held_by", "program", "interval_months")
CALIBRATION_LAYOUT = ("calibrated_on", "result", "due_on", "as_found", "adjusted", "provider", "certificate", "accreditation")


# --- the list's filters ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TeFilters:
    q: str = ""
    standing: str = ""  # rules.FILTERS' keys
    status: str = ""  # "" (not retired), a Status, or "all"
    kind: str = ""
    held_by: str = ""  # a technician's id, NOBODY, or ""

    @property
    def any(self) -> bool:
        return bool(self.q or self.standing or self.status or self.kind or self.held_by)


def parse_te_filters(params) -> TeFilters:
    """The list's filters from the address: unknown values are dropped; the search is a tag or serial, NUL characters taken out (a
    database refuses them)."""
    def one_of(name, allowed):
        value = params.get(name, "")
        return value if value in allowed else ""

    q = " ".join(str(params.get("q", "")).replace("\x00", "").split())[:TAG_MAX * 2]
    held = params.get("held_by", "")
    held = held if held == NOBODY or parse_uuid(held) is not None else ""
    return TeFilters(q=q, standing=one_of("standing", rules.FILTERS), status=one_of("status", {v for v, _ in STATUS_FILTERS}),
                     kind=one_of("kind", Kind.values), held_by=held)


def holder_choices(current=None) -> list[tuple[str, str]]:
    """The technicians an item may be held by: the active ones, and the one it has (who may have left since)."""
    techs = list(Technician.objects.filter(is_active=True).order_by("name"))
    if current is not None and all(t.pk != current.pk for t in techs):
        techs.append(current)
    return [("", "Nobody (the department's)")] + [(str(t.pk), t.name if t.is_active else f"{t.name} (no longer active)") for t in techs]


def holder_filter_choices() -> list[tuple[str, str]]:
    """The list's held-by filter: the active technicians and anyone holding an item, then nobody."""
    from django.db.models import Q

    techs = Technician.objects.filter(Q(is_active=True) | Q(test_equipment__isnull=False)).distinct().order_by("name")
    return [(str(t.pk), t.name) for t in techs] + [(NOBODY, "Nobody")]


# --- the forms -------------------------------------------------------------------------------------------------------------------

def _text(label, *, limit, help_text="", placeholder="", required=True):
    attrs = {"autocomplete": "off", "maxlength": limit}
    if placeholder:
        attrs["placeholder"] = placeholder
    if required:
        attrs["required"] = True
    return forms.CharField(label=label, required=False, help_text=help_text, strip=False, widget=forms.TextInput(attrs=attrs))


def _day(label, help_text=""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs=DATE))


def _select(label, choices, help_text=""):
    """A choice kept as typed: the service refuses one not listed, in its words, with the other problems."""
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.Select(choices=choices))


def _radios(label, choices, help_text=""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.RadioSelect(choices=choices))


class ServiceForm(forms.Form):
    """A form whose save is a service call: its refusals (a ValidationError keyed by field) on the fields they name, anything else at
    the top. The fields are text as typed (never refused here, beyond a NUL character): the service checks them all at once."""

    def add_service_errors(self, error):
        if not hasattr(error, "error_dict"):
            self.add_error(None, getattr(error, "messages", None) or [str(error)])
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)

    def raw(self, name):
        value = self.cleaned_data.get(name)
        return value.strip() if isinstance(value, str) else value

    layout: tuple = ()

    def focus_first_error(self) -> str:
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so make that the first field with an error (in
        `layout`, the order the modal lays them out). Returns its name ("" for none)."""
        first = next((name for name in self.layout if name in self.errors), "")
        for f in self.fields.values():
            f.widget.attrs.pop("autofocus", None)
        if first in self.fields:
            self.fields[first].widget.attrs["autofocus"] = True
        return first


class CertificateFields(forms.Form):
    """A certificate's fields (record_calibration's): the day on it, the result, the due day (blank: the item's interval after the
    day), as found, adjusted, the provider, its number, and the accreditation mark."""
    calibrated_on = _day("Calibration day on the certificate")
    result = _radios("Result", Result.choices)
    due_on = _day("Due day")
    as_found = _select("As found", [CHOOSE, *AsFound.choices])
    adjusted = forms.BooleanField(label="Adjusted by the provider", required=False)
    provider = _text("Provider", limit=TEXT_MAX, help_text="The lab or manufacturer that issued the certificate.",
                     placeholder="e.g. Fluke Biomedical")
    certificate = _text("Certificate number", limit=40, help_text="As it is printed, without spaces.", placeholder="e.g. 1234567-A")
    accreditation = _radios("Accreditation mark", list(MARK_WORDS.items()),
                            help_text="Cadence records what the certificate shows; it cannot check a lab's accreditation scope.")

    def certificate_kwargs(self) -> dict:
        d = self.cleaned_data
        return {"calibrated_on": (d.get("calibrated_on") or "").strip(), "due_on": (d.get("due_on") or "").strip() or None,
                "provider": d.get("provider") or "", "certificate": (d.get("certificate") or "").strip(),
                "accreditation": d.get("accreditation") or "", "as_found": d.get("as_found") or "", "adjusted": bool(d.get("adjusted")),
                "result": d.get("result") or ""}


class ItemForm(ServiceForm):
    """Add test equipment and Change: the item's details. The view takes out what may not change (the tag once a calibration or work
    is on record, the interval below Decide once a certificate is on file)."""
    tag = _text("Tag", limit=TAG_MAX, help_text="The CE control number on its sticker. Devices and test equipment share the tags.",
                placeholder="e.g. TE-0042")
    kind = _select("Kind", [CHOOSE, *Kind.choices])
    manufacturer = _text("Manufacturer", limit=TEXT_MAX, placeholder="e.g. Fluke Biomedical")
    model = _text("Model", limit=TEXT_MAX, placeholder="e.g. ESA612")
    serial = _text("Serial number", limit=SERIAL_MAX, help_text="As its certificates name it.")
    program = _radios("Certificates", PROGRAM_CHOICES)
    interval_months = forms.CharField(label="Interval (months)", required=False,
                                      help_text=f"How often it goes for a certificate: {INTERVAL_MIN} to {INTERVAL_MAX} months.",
                                      widget=forms.NumberInput(attrs={"min": INTERVAL_MIN, "max": INTERVAL_MAX, "inputmode": "numeric"}))
    held_by = _select("Held by", [], help_text="Whose kit it is, if anyone's.")
    layout = ITEM_LAYOUT

    def __init__(self, *args, holder=None, **kwargs):
        kwargs.setdefault("auto_id", "te-%s")
        super().__init__(*args, **kwargs)
        self.fields["held_by"].widget.choices = holder_choices(holder)
        self.locks = {}  # Change: the fields not offered, with why (views_testequipment._edit_form)

    def item_kwargs(self) -> dict:
        """The item's fields as typed, for the ones this form still has."""
        return {name: self.raw(name) or "" for name in ITEM_FIELDS if name in self.fields}


class NewItemForm(ItemForm, CertificateFields):
    """Add test equipment, with its latest certificate optional (services.add_test_equipment(calibration=...)): left empty, the item is
    added with no calibration on file. Any certificate field filled (has_certificate: never the accreditation mark alone) sends the
    certificate, and the service checks all of it."""
    layout = (*ITEM_LAYOUT, *CALIBRATION_LAYOUT)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_on"].help_text =("Leave it blank for the interval after the calibration day, or type the day the certificate "
                                           "or label shows (at most the end of the month the interval ends in).")

    def has_certificate(self) -> bool:
        """A certificate is being entered when a day, the provider, its number, as found (a select with a blank choice), adjusted, or
        Failed is given. Never by the accreditation mark alone: it is a radio a browser cannot untick, so a stray click would make the
        optional certificate required (review fix)."""
        d = self.cleaned_data
        typed = any((d.get(name) or "").strip() for name in ("calibrated_on", "due_on", "provider", "certificate", "as_found"))
        return typed or bool(d.get("adjusted")) or d.get("result") == Result.FAILED

    def calibration(self) -> dict | None:
        return self.certificate_kwargs() if self.has_certificate() else None


def new_item_initial() -> dict:
    return {"program": Program.CALIBRATION, "interval_months": INTERVAL_DEFAULT, "result": Result.PASSED}


class CalibrationForm(ServiceForm, CertificateFields):
    """Record calibration. The due day comes prefilled with the item's interval after the calibration day (`due_default`, hidden, is
    the day prefilled): left as prefilled, the service's default is used (it follows a calibration day changed meanwhile, and a failed
    certificate has none); typed, it is the certificate's or label's day, and the service refuses one past the cap."""
    due_default = forms.CharField(required=False, widget=forms.HiddenInput)
    layout = CALIBRATION_LAYOUT

    def __init__(self, *args, item=None, **kwargs):
        kwargs.setdefault("auto_id", "tec-%s")
        super().__init__(*args, **kwargs)
        self.item = item
        what = rules.noun(item) if item is not None else "Calibration"
        self.fields["calibrated_on"].label = f"{what} day on the certificate"
        if item is not None:  # the due day's prefill follows the day (web/_te_due.html, #tec-due)
            self.fields["calibrated_on"].widget.attrs.update({
                "hx-get": reverse("web:test_equipment_calibrate", args=[item.tag]), "hx-trigger": "change", "hx-target": "#tec-due",
                "hx-swap": "outerHTML", "hx-include": "#tec-form"})

    def service_kwargs(self) -> dict:
        out = self.certificate_kwargs()
        prefilled = (self.cleaned_data.get("due_default") or "").strip()
        if out["due_on"] is not None and out["due_on"] == prefilled:
            out["due_on"] = None  # as prefilled: the interval after the day typed (none for a failed certificate)
        return out


def parse_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class DueHint:
    """The due day's prefill and hint for a calibration day: the default (the interval after it) and the cap (the end of the month
    the interval ends in), or nothing when the day does not read as one or lies beyond the calendar's end."""
    default: date | None
    cap: date | None
    words: str


def due_hint(item, calibrated_on: date | None) -> DueHint:
    interval = item.interval_months
    what = rules.noun(item).lower()
    if calibrated_on is None:
        return DueHint(None, None, f"{interval} months after the {what} day unless the certificate or label shows another day.")
    try:
        default, cap = default_due(calibrated_on, interval), due_cap(calibrated_on, interval)
    except (OverflowError, ValueError):
        return DueHint(None, None, f"{interval} months after the {what} day unless the certificate or label shows another day.")
    return DueHint(default, cap, f"Prefilled {interval} months after the {what} day. Type the day the certificate or label shows if it "
                                 f"differs: at most {fmt(cap)}. A failed {what} has no due day.")


def calibration_initial(item, today: date, last=None) -> dict:
    """Record calibration's values: today, passed, the due day prefilled, and the provider of the item's latest certificate."""
    hint = due_hint(item, today)
    initial = {"calibrated_on": today.isoformat(), "result": Result.PASSED, "due_on": hint.default.isoformat() if hint.default else "",
               "due_default": hint.default.isoformat() if hint.default else ""}
    if last is not None:
        initial["provider"] = last.provider
    return initial


class ReviewForm(ServiceForm):
    """The tolerance review's decision (services.decide_review): the basis, the facility's quality record (optional), the day it was
    decided, and `version`, the work list the decider was shown (services.review_version): refused when the list moved since."""
    basis = _radios("Decision", Basis.choices)
    quality_reference = _text("Quality record number (optional)", limit=40, required=False,
                              help_text="The facility's quality record of this review, if any, without spaces.")
    decided_on = _day("Decided on")
    version = forms.CharField(required=False, widget=forms.HiddenInput)
    layout = ("basis", "decided_on", "quality_reference")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ter-%s")
        super().__init__(*args, **kwargs)

    def service_kwargs(self) -> dict:
        return {"basis": self.raw("basis") or "", "quality_reference": self.raw("quality_reference") or "",
                "decided_on": self.raw("decided_on") or "", "version": self.raw("version") or ""}
