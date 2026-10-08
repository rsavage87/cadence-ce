"""
Input parsing for the Incidents screen (slice 28): the list's filters, and the forms of the record modal, the facts, the decision,
the reports, a hold's custody and release. Same rules as forms.py: unknown filter values are dropped, choices are the models' own,
and nothing here is free text (apps.incidents.models: no field on an incident is; the event report number is a pattern the service
checks). The forms read and shape what was typed; apps.incidents.services decides, and its refusals come back on the field they name.
"""
from dataclasses import dataclass

from django import forms
from django.core.exceptions import ValidationError

from apps.incidents.models import (
    RELEASE_CHOICES,
    Accessories,
    Affected,
    AwareChange,
    Basis,
    DecidedBy,
    EventLog,
    Finding,
    Outcome,
    Release,
    Status,
)
from apps.incidents.services import EVENT_REFERENCE_MAX

DATE = {"type": "date"}
NOT_RECORDED = ("", "Not recorded yet")
# The list's status filter: open and closed (the default: one recorded in error is counted nowhere), or one of them, or all.
STATUS_FILTERS = [("", "Open and closed"), (Status.OPEN, "Open"), (Status.CLOSED, "Closed"), (Status.IN_ERROR, "Recorded in error"),
                  ("all", "All, recorded in error too")]
EVENT_REFERENCE_HELP = ("The number your event reporting system gave. Never a patient's name, MRN, or account number. It can be added "
                        "later.")
AWARE_HELP = "The 10-work-day clock starts that day, not when CE was told. Leave it empty if it is the day it happened."
NEW, NONE = "new", "none"  # the record modal's investigation: open a new work order, or none (only without a hold)


@dataclass(frozen=True)
class IncidentFilters:
    status: str = ""  # "" (open and closed), open, closed, in_error, all
    year: int | None = None


def parse_incident_filters(params, years) -> IncidentFilters:
    """The list's filters from the address: a status from STATUS_FILTERS, a year among `years` (the ones with incidents)."""
    status = params.get("status", "")
    status = status if status in {v for v, _ in STATUS_FILTERS} else ""
    try:
        year = int(params.get("year", ""))
    except ValueError:
        year = None
    return IncidentFilters(status=status, year=year if year in years else None)


def _date(label, *, required=True, help_text="", message="Enter a date."):
    return forms.DateField(label=label, required=required, help_text=help_text, widget=forms.DateInput(attrs=DATE, format="%Y-%m-%d"),
                           error_messages={"required": message, "invalid": "Enter a date."})


def _choice(label, choices, *, required=True, message="Choose one.", radio=False, blank=None):
    return forms.ChoiceField(label=label, required=required, choices=([blank] if blank else []) + list(choices),
                             widget=forms.RadioSelect if radio else forms.Select,
                             error_messages={"required": message, "invalid_choice": "Choose one of the choices listed."})


class ServiceForm(forms.Form):
    """A form whose save is a service call: its refusals (ValidationError keyed by field, or PermissionDenied's words) on the fields
    they name, anything else at the top."""

    def add_service_errors(self, error):
        if not hasattr(error, "error_dict"):
            self.add_error(None, getattr(error, "messages", None) or [str(error)])
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)

    def add_refusal(self, message: str):
        self.add_error(None, message)


def _reference_field():
    return forms.CharField(label="Event report number", required=False, max_length=EVENT_REFERENCE_MAX, help_text=EVENT_REFERENCE_HELP,
                           widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false", "placeholder": "e.g. EV-2026-0412"}))


class RecordForm(ServiceForm):
    """Record incident. The device is the hidden `asset` (a tag); `investigation` is an open repair's number to adopt as the
    investigation, NEW to open one, or NONE (only when the device is not held: holding always comes with an investigation). The
    view fills in the device and the investigation's choices."""
    asset = forms.CharField(widget=forms.HiddenInput, required=False)
    occurred_on = _date("It happened on", message="Enter the day it happened.")
    aware_on = _date("Clinical staff first knew on", required=False, help_text=AWARE_HELP)
    outcome = _choice("Outcome", Outcome.choices, message="Choose the outcome.")
    affected = _choice("Who was affected", Affected.choices, message="Choose who was affected.", blank=("", "Choose…"))
    hold = forms.BooleanField(required=False, label="Hold the device as evidence")
    investigation = forms.CharField(required=False)
    accessories = _choice("Accessories and disposables in use", Accessories.choices, required=False, blank=NOT_RECORDED)
    event_log = _choice("The device's event log", EventLog.choices, required=False, blank=NOT_RECORDED)
    event_reference = _reference_field()

    def __init__(self, *args, asset=None, adoptable=(), can_open=True, **kwargs):
        kwargs.setdefault("auto_id", "inc-%s")
        super().__init__(*args, **kwargs)
        self.asset_obj = asset
        self.adoptable = {wo.number: wo for wo in adoptable}
        self.can_open = can_open

    def clean_asset(self):
        if self.asset_obj is None:
            raise forms.ValidationError("Choose the device from the list first.")
        return self.asset_obj

    def add_service_errors(self, error):
        """record_incident's refusal of the work order to adopt (keyed work_order) shows under the investigation's choices."""
        if hasattr(error, "error_dict") and "work_order" in error.error_dict:
            error = ValidationError({("investigation" if k == "work_order" else k): v for k, v in error.message_dict.items()})
        super().add_service_errors(error)

    def clean(self):
        data = super().clean()
        choice = (data.get("investigation") or "").strip()
        if not choice:
            choice = NEW if self.can_open or data.get("hold") else NONE
        elif choice not in (NEW, NONE) and choice not in self.adoptable:
            self.add_error("investigation", f"{choice} can no longer be taken as the investigation (it closed, or another incident took "
                                            "it): choose again.")
        if choice == NONE and data.get("hold"):
            self.add_error("investigation", "Holding the device always comes with an investigation: take the request it was reported as, "
                                            "or open one.")
        data["investigation"] = choice
        return data

    def service_kwargs(self) -> dict:
        """record_incident's arguments from the cleaned form."""
        d = self.cleaned_data
        choice = d["investigation"]
        return {"asset": d["asset"], "occurred_on": d["occurred_on"], "aware_on": d.get("aware_on"), "outcome": d["outcome"],
                "affected": d["affected"], "event_reference": d.get("event_reference", ""), "hold": bool(d.get("hold")),
                "work_order": self.adoptable.get(choice), "open_work_order": choice == NEW, "accessories": d.get("accessories", ""),
                "event_log": d.get("event_log", "")}


class FactsForm(ServiceForm):
    """Edit facts (update_facts). Once decided, outcome and affected change only by deciding again: disabled. `aware_reason` is asked
    of a decider (moving the day clinical staff first knew later needs one)."""
    occurred_on = _date("It happened on", message="Enter the day it happened.")
    aware_on = _date("Clinical staff first knew on", message="Enter the day the facility's clinical staff first knew.",
                     help_text="The 10-work-day clock starts that day, not when CE was told.")
    aware_reason = _choice("If that day moves later, why", AwareChange.choices, required=False, blank=("", "It does not move later"))
    outcome = _choice("Outcome", Outcome.choices, message="Choose the outcome.")
    affected = _choice("Who was affected", Affected.choices, message="Choose who was affected.")
    event_reference = _reference_field()
    accessories = _choice("Accessories and disposables in use", Accessories.choices, required=False, blank=NOT_RECORDED)
    event_log = _choice("The device's event log", EventLog.choices, required=False, blank=NOT_RECORDED)

    FIELDS = ("occurred_on", "aware_on", "outcome", "affected", "event_reference", "accessories", "event_log")

    def __init__(self, *args, incident, can_decide: bool, **kwargs):
        kwargs.setdefault("auto_id", "incf-%s")
        kwargs.setdefault("initial", {f: getattr(incident, f) for f in self.FIELDS})
        super().__init__(*args, **kwargs)
        self.decided = incident.reportable is not None
        if self.decided:
            for name in ("outcome", "affected"):
                self.fields[name].disabled = True
        if not can_decide:
            del self.fields["aware_reason"]

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        out = {f: d.get(f) for f in self.FIELDS}
        out["aware_reason"] = d.get("aware_reason", "")
        return out


# The decision's final outcome: never "not known yet".
FINAL_OUTCOMES = [(v, label) for v, label in Outcome.choices if v != Outcome.UNKNOWN]


# Review fix: once decided, the outcome and who was affected change only by deciding again (services.update_facts says so).
DECIDE_AFFECTED_HELP = ("As it is now known. New information about who was affected, or about the outcome, is decided again here: the "
                        "report clock follows the decision.")


class DecideForm(ServiceForm):
    """The reportability decision (decide). Who was affected comes with the outcome, prefilled with the incident's (services.decide
    checks the pair: a harm outcome needs someone affected, a report a patient of the facility or a staff member on duty). The event
    report number is asked here only when the incident has none: the decision points to the deliberations there."""
    outcome = _choice("The outcome as it is now known", FINAL_OUTCOMES, message="Choose the outcome as it is now known.",
                      blank=("", "Choose…"))
    affected = _choice("Who was affected", Affected.choices, required=False)  # not sent: as recorded (services.decide's None)
    basis = _choice("The decision", Basis.choices, message="Choose the basis for the decision.", radio=True)
    decided_on = _date("Decided on", message="Enter the day it was decided.")
    decided_by = _choice("Decided by", DecidedBy.choices, message="Choose who decided.", blank=("", "Choose…"))
    event_reference = _reference_field()

    def __init__(self, *args, incident, **kwargs):
        kwargs.setdefault("auto_id", "incd-%s")
        super().__init__(*args, **kwargs)
        self.fields["affected"].help_text = DECIDE_AFFECTED_HELP
        if incident.event_reference:
            del self.fields["event_reference"]
        else:
            self.fields["event_reference"].help_text = ("Where the deliberations on this decision are: the number your event reporting system "
                                                        "gave. Never a patient's name, MRN, or account number.")


class ReportsForm(ServiceForm):
    """The reports sent (record_reports): one report number, the day each copy went out."""
    fda_reported_on = _date("Sent to the FDA on", required=False)
    manufacturer_reported_on = _date("Sent to the manufacturer on", required=False)
    report_number = forms.CharField(label="Report number", required=False, max_length=20,
                                    help_text="The facility's 10-digit number, the year, and a 4-digit sequence: 0123456789-2026-0001.",
                                    widget=forms.TextInput(attrs={"autocomplete": "off", "inputmode": "numeric", "placeholder": "0123456789-2026-0001"}))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "incr-%s")
        super().__init__(*args, **kwargs)


class FindingForm(ServiceForm):
    finding = _choice("The device evaluation found", Finding.choices, message="Choose what the device evaluation found.",
                      blank=("", "Not recorded yet"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["finding"].widget.attrs["aria-label"] = self.fields["finding"].label


class DayForm(ServiceForm):
    """A hold's custody: the day it went to the manufacturer, or came back."""
    on = _date("On", message="Enter the day.")

    def __init__(self, *args, field: str, **kwargs):
        kwargs.setdefault("auto_id", "incu-%s")
        super().__init__(*args, **kwargs)
        self.service_field = field  # sent_on / back_on: the service's key for this date

    def add_service_errors(self, error):
        if hasattr(error, "error_dict"):
            for key, messages in error.message_dict.items():
                self.add_error("on" if key == self.service_field else None, messages)
            return
        super().add_service_errors(error)


# What each way of ending a hold does, under its label in the release modal.
RELEASE_WORDS = {
    Release.RETURN_TO_USE: "The device goes back in service, unless another incident still holds it, it waits for its incoming inspection, "
                           "or a repair holds it out. Needs the investigation done, the decision and any required reports recorded, and no "
                           "PM past due.",
    Release.KEEP_OUT: "The hold ends and the device stays out of service: take it from there in its drawer (a PM, a repair, retire it).",
    Release.KEPT_BY_MANUFACTURER: "The manufacturer keeps the device (it is with them now). The hold ends; it stays out of service.",
}


class ReleaseForm(ServiceForm):
    release = _choice("How the hold ends", [(v, Release(v).label) for v in RELEASE_CHOICES], radio=True,
                      message="Choose how the hold ends: returned to use, kept out of service, or kept by the manufacturer.")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "incl-%s")
        super().__init__(*args, **kwargs)
