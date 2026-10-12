"""
Input parsing for completing a work order (slice 15, web/_wo_complete.html). The steps depend on the device's PM procedure, so
their fields (step_<n>: pass, fail, or na; reading_<n>) are built in __init__ from the checklist the modal shows. The rules live
in apps.workorders.completion; this form passes on what was typed, and puts the service's errors back on the fields they name
(the service keys them by these same names).

Slice 24, on a phone: a reading box brings up the number pad (inputmode="decimal") when its step reads a number (reads_number), and
an optional Hours box logs time with the completion (offered by the view when there is someone to log it for).

Slice 25: a PM completed after its due date (completion.completes_late) gets an optional "Why was it late?" select (late_reason,
LateReason's choices). Blank keeps a reason already recorded; the blank choice says which. The view says where it sits: before the
result for a life-support or high-risk PM (late_first), else after the hours. Slice 27: "after its due date" is after its on-time
window (apps.pm.windows: the due date unless the facility chose otherwise); the view decides whether the select is offered.

Slice 26: an incoming inspection (is_inspection) takes its checklist like a PM (every step or none: completion's rule) and its result
(inspection_result, InspectionResult: required while the device waits for it, optional otherwise; the service says so). A failed
one's tag-out (a device put in use before its inspection) is taken from the post only when the modal showed it; otherwise its
default (on), which the service applies only to a device in use.

Slice 30: a repair (is_repair) takes how it ended (repair_check, RepairCheck: text, completion checks the value and which outcomes the
user may choose) and, for checked and ready for use, the post-repair checklist's steps (completion.procedure_for); the vendor's
service report number (checks_reference) goes to the service only with checked by the vendor, and the steps only with checked and
ready for use, so values left on screen under another choice are never sent. Wave 2: the outcome chosen to begin with
(`repair_default`: checked and ready for use, unless the vendor's report is offered), the steps never N/A (repair_checks.NEVER_NA: no
N/A choice, rows()' never_na), and "If a check fails" (keep_open_kwargs, for completion.keep_open: the steps as typed and `hold`, the
hold-out box, ticked by default and taken from the post only when the modal showed it: shown_hold).

Slice 31, vendor service of any type (apps.vendors): who did the work, one select (vendor_engineer: an engineer's id; "" not named on
the report; OTHER, "Someone else", with the name typed in engineer_name; KEEP_CHOICE, a scoped vendor account's "Another company's
engineer", left as stored), offered when the view says so (offer_engineer, the engineer stored as `engineer_initial`), and the vendor's
report number (checks_reference) on any vendor job for a user who sees the whole facility (offer_reference). Slice 30 sent the number
only with checked by the vendor; it is now sent whatever the outcome (the box shows on every vendor job), and completion still requires
it with checked by the vendor. engineer_kwargs turns the select into the service's vendor_engineer and engineer_name (EngineerForm,
the drawer's Change, reads it too); "Someone else" with no name is refused here, keyed engineer_name, since the service would read an
empty name as not named.
"""
import re

from django import forms
from django.core.exceptions import ValidationError

from apps.vendors import rules as vendor_rules
from apps.workorders import completion, repair_checks
from apps.workorders.models import InspectionResult, LateReason, PmResult, RepairCheck

RESOLUTION_PLACEHOLDER = "What was found and done. No patient information."
PM_RESOLUTION_PLACEHOLDER = "Optional when the PM passed. What was found, repaired, or failed. No patient information."
INSPECTION_RESOLUTION_PLACEHOLDER = ("Optional when it passed with every step recorded. What failed, or what was checked (a vendor's "
                                     "acceptance report). No patient information.")
# The overall result's choices, each with a line saying when it fits (the modal's radio cards)
RESULT_CHOICES = [
    (PmResult.PASS, "Pass", "Every step passed or did not apply"),
    (PmResult.PASS_MINOR_REPAIR, "Pass with minor repair", "Put right during the PM; say what in the resolution"),
    (PmResult.FAIL, "Fail", "Needs a repair before it is relied on"),
]
STEP_CHOICES = [(completion.PASS, "Pass"), (completion.FAIL, "Fail"), (completion.NA, "N/A")]
# The fields after the checklist, in the modal's order: a form sent back with errors focuses the first one on screen. Slice 25: the
# late reason comes first (late_first) or last (focus_first_error).
LAYOUT_TAIL = ("pm_result", "inspection_result", "resolution", "open_repair", "hours")
LAYOUT_REPAIR = ("repair_check",)  # slice 30: a repair's outcome comes before its checklist
LAYOUT_VENDOR = ("vendor_engineer", "engineer_name", "checks_reference")  # slice 31: then who did the vendor's work, and their report
OTHER = "other"  # slice 31: the select's "Someone else" (the name typed in engineer_name)
KEEP_CHOICE = "keep"  # slice 31: a scoped vendor account's "Another company's engineer": the engineer stored stays
LATE_BLANK = "Not recorded"
LATE_KEEP = "Keep the reason recorded: {label}"
# What a step records reads as a number when it names a limit or a unit ("leakage µA, limit 100", "Ω", "mmHg"). Anything else
# ("serial number", or a reading that says nothing of what) keeps the keyboard: the number pad has no letters. So does a reading that
# may be below zero ("±0.5 °C", "-10 to 10 mV"): a phone's number pad has no minus sign.
# Units a reading never goes below zero in. Not °C, °F, %, mmHg, cmH2O, mV, V, kPa, psi, mbar, or dB: a freezer reads -21 °C, a pump's
# volume accuracy -3.2 %, a transducer zero -1 mmHg, and the phone's number pad has no minus key (review fix).
UNITS = frozenset(("µa ua ma amps kv ω ohm ohms mω kω sec secs seconds min mins minutes ms hz bpm "
                   "ml l/min lpm ml/h ml/hr ml/min j joules w watts lux kg lb lbs").split())
_WORD = re.compile(r"[^\s,;:()]+")
_NEGATIVE = re.compile(r"±|(^|[\s(,;:])[-−]\s?\d")


def reads_number(measure) -> bool:
    """Whether a step's reading is a number (its box brings up the number pad). `measure` as completion.checklist_of gives it."""
    if not isinstance(measure, str):
        return False
    text = measure.lower()
    if _NEGATIVE.search(text):
        return False
    return any(word in UNITS for word in _WORD.findall(text))  # a unit, never digits alone ("software version 2.1.3a" needs letters)


def engineer_kwargs(value, name) -> dict:
    """The service's vendor_engineer and engineer_name for what the select sent (slice 31): an engineer's id; "" (not named on the
    report: None); OTHER with the name typed; KEEP_CHOICE ({}: the engineer stored stays). Raises ValidationError keyed engineer_name
    for Someone else with no name (the name's own rule is the service's: vendors.rules.clean_engineer_name)."""
    value, name = str(value or "").strip(), str(name or "").strip()
    if value == KEEP_CHOICE:
        return {}
    if value == OTHER:
        if not name:
            vendor_rules.clean_engineer_name(name, "engineer_name")  # raises, in the rule's words
        return {"vendor_engineer": None, "engineer_name": name}
    return {"vendor_engineer": value or None, "engineer_name": ""}


def service_errors(form, error):
    """A service's ValidationError on the fields it names; anything else at the top (EngineerForm and CompleteForm)."""
    if not hasattr(error, "error_dict"):
        form.add_error(None, error.messages)
        return
    for key, messages in error.message_dict.items():
        if key in form.fields:
            form.add_error(key, messages)
        else:
            form._errors.setdefault(key, form.error_class()).extend(messages)  # "checklist": shown above the steps


class EngineerForm(forms.Form):
    """The drawer's Change (slice 31, views_wo_engineer): the completion modal's select, for workorders.services.set_vendor_engineer."""
    vendor_engineer = forms.CharField(required=False)
    engineer_name = forms.CharField(required=False)

    def clean(self):
        data = super().clean()
        self.choice = {}
        try:
            self.choice = engineer_kwargs(data.get("vendor_engineer"), data.get("engineer_name"))
        except ValidationError as e:
            self.add_error(None, e)
        return data

    def add_service_errors(self, error):
        service_errors(self, error)


class CompleteForm(forms.Form):
    resolution = forms.CharField(required=False, strip=False, widget=forms.Textarea(attrs={"rows": 3, "maxlength": completion.RESOLUTION_MAX}))
    # Text, not a number field: apps.workorders.costs says what a valid number of hours is. inputmode brings up the number pad.
    hours = forms.CharField(required=False, widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off", "placeholder": "e.g. 1.5",
                                                                          "aria-describedby": "wc-hours-for"}))
    pm_result = forms.CharField(required=False)
    # Slice 26: text, not a choice field: completion checks the value (a refusal keyed inspection_result, in its words)
    inspection_result = forms.CharField(required=False)
    # Slice 30: a repair's outcome (RepairCheck) and the vendor's service report number (slice 31: on any vendor job); text, completion
    # checks them
    repair_check = forms.CharField(required=False)
    checks_reference = forms.CharField(required=False, widget=forms.TextInput(attrs={"maxlength": completion.REFERENCE_MAX, "autocomplete": "off",
                                                                                    "placeholder": "e.g. FSO-118204"}))
    open_repair = forms.BooleanField(required=False, initial=True)
    tag_out = forms.BooleanField(required=False, initial=True)
    # Slice 30, "If a check fails" on a repair: keep it open holding the device out (completion.keep_open's hold), ticked by default
    hold = forms.BooleanField(required=False, initial=True)
    shown_hold = forms.CharField(required=False)
    signature = forms.CharField(required=False, widget=forms.HiddenInput)
    # Which failed-PM options were on screen when the modal was filled in (the template posts these next to each checkbox): the
    # device can change while the modal is open, and an option not shown was never declined.
    shown_open_repair = forms.CharField(required=False)
    shown_tag_out = forms.CharField(required=False)
    # Slice 25: text, not a choice field: completion checks the value (a refusal keyed late_reason, in its words). Choices in __init__.
    late_reason = forms.CharField(required=False, widget=forms.Select(attrs={"aria-describedby": "wc-late-hint"}))
    # Slice 31: who did the vendor's work (engineer_kwargs reads them; web/_wo_engineer_select.html draws them)
    vendor_engineer = forms.CharField(required=False)
    engineer_name = forms.CharField(required=False)

    def __init__(self, *args, steps=(), is_pm=False, is_inspection=False, kept_pass=False, offer_open_repair=False, offer_tag_out=False,
                 offer_hours=False, offer_late_reason=False, late_first=False, late_recorded="", is_repair=False, repair_default="",
                 offer_engineer=False, engineer_initial="", offer_reference=False, reference_initial="", **kwargs):
        """`steps`: the checklist the modal shows, as completion.checklist_of gives it. The two offers say whether the failed-PM
        options are on screen: an option that is not offered is not taken from the post (a new repair is opened; no tag-out).
        `offer_hours`: the Hours box is on screen; without it no hours are taken from the post. `offer_late_reason` (slice 25): the
        "Why was it late?" select is on screen (before the result when `late_first`); `late_recorded` names the reason already on
        record, which its blank choice keeps. Slice 26: `is_inspection`, an incoming inspection (its result instead of a PM's, and a
        failed one's tag-out under `offer_tag_out`); `kept_pass`, one that passed and whose device no longer waits (it stays
        passed: Passed is chosen to begin with). Slice 30: `is_repair`, a repair (its outcome, and its checks after it as `steps`);
        `repair_default`, the outcome chosen to begin with ("" for none). Slice 31: `offer_engineer`, vendor service (who did the work,
        the engineer stored as `engineer_initial`: the select's value to begin with); `offer_reference`, the vendor's report number box
        (any vendor job, for a user who sees the whole facility), with the number stored as `reference_initial` (a reopened job)."""
        kwargs.setdefault("auto_id", "wc-%s")
        super().__init__(*args, **kwargs)
        self.steps, self.is_pm, self.is_inspection = list(steps), is_pm, is_inspection and not is_pm
        self.is_repair = is_repair and not (is_pm or is_inspection)
        if not self.is_repair:
            del self.fields["repair_check"], self.fields["hold"], self.fields["shown_hold"]
        elif repair_default:
            self.fields["repair_check"].initial = repair_default
        self.offer_engineer, self.offer_reference, self.engineer = offer_engineer, offer_reference, {}
        if offer_engineer:
            self.fields["vendor_engineer"].initial = engineer_initial
        else:
            del self.fields["vendor_engineer"], self.fields["engineer_name"]
        if not offer_reference:
            del self.fields["checks_reference"]
        elif reference_initial:
            self.fields["checks_reference"].initial = reference_initial
        if not self.is_inspection:
            del self.fields["inspection_result"]
        elif kept_pass:
            self.fields["inspection_result"].initial = InspectionResult.PASSED
        self.offer_open_repair, self.offer_tag_out, self.offer_hours = offer_open_repair, offer_tag_out, offer_hours
        self.offer_late_reason, self.late_first = offer_late_reason, offer_late_reason and late_first
        if not offer_hours:
            del self.fields["hours"]
        if offer_late_reason:
            blank = LATE_KEEP.format(label=late_recorded) if late_recorded else LATE_BLANK
            self.fields["late_reason"].widget.choices = [("", blank), *LateReason.choices]
        else:
            del self.fields["late_reason"]
        self.first_error = ""
        for n in range(1, len(self.steps) + 1):
            self.fields[f"step_{n}"] = forms.CharField(required=False)
            self.fields[f"reading_{n}"] = forms.CharField(required=False)
        self.fields["signature"].initial = completion.checklist_signature(self.steps)
        attrs = self.fields["resolution"].widget.attrs
        attrs["placeholder"] = (PM_RESOLUTION_PLACEHOLDER if is_pm else INSPECTION_RESOLUTION_PLACEHOLDER if self.is_inspection
                                else RESOLUTION_PLACEHOLDER)
        if not (is_pm or self.is_inspection or self.is_repair):  # a PM's or an inspection's checklist, or a repair's outcome, comes first
            attrs["autofocus"] = True

    @classmethod
    def filled(cls, params, *, fill: str = "", **kwargs) -> "CompleteForm":
        """An unbound form showing `params` (the modal's values, sent back to re-render it) with every unanswered step set to
        `fill`: "Mark the rest pass" without saving anything."""
        form = cls(**kwargs)
        initial = {name: params.get(name, "") for name in form.fields
                   if name not in ("open_repair", "tag_out", "signature", "shown_open_repair", "shown_tag_out", "hold", "shown_hold")}
        initial.update(open_repair="open_repair" in params, tag_out="tag_out" in params)
        if "hold" in form.fields and params.get("shown_hold"):  # slice 30: the hold-out box as it was on screen (else its default, on)
            initial["hold"] = "hold" in params
        if params.get("signature"):  # the checklist the values were typed against, so a revision since is still caught on save
            initial["signature"] = params["signature"]
        if fill in dict(STEP_CHOICES):
            for n in range(1, len(form.steps) + 1):
                if not initial.get(f"step_{n}"):
                    initial[f"step_{n}"] = fill
        form.initial.update(initial)
        return form

    check_engineer = True  # slice 31: Keep open (completion.keep_open names no engineer) reads the form without the select

    def clean(self):
        data = super().clean()
        if self.offer_engineer and self.check_engineer:  # slice 31
            try:
                self.engineer = engineer_kwargs(data.get("vendor_engineer"), data.get("engineer_name"))
            except ValidationError as e:
                self.add_error(None, e)
        return data

    # --- what the template shows -----------------------------------------------------------------------------------------

    def _value(self, name) -> str:
        value = self[name].value()
        return "" if value is None else str(value)

    def rows(self) -> list[dict]:
        """Each step as the modal shows it; `prompt` is the line under it ("Record µA, limit 100"; slice 26: the incoming checklist's
        "Record leakage µA" already says so)."""
        out = []
        for n, (text, measure) in enumerate(self.steps, 1):
            shown = "" if measure in (None, True) else str(measure)
            out.append({"n": n, "text": text, "measured": measure is not None, "measure": shown,
                        "never_na": self.is_repair and n - 1 in repair_checks.NEVER_NA,  # slice 30: no N/A choice on these
                        "prompt": shown if shown.lower().startswith("record ") else f"Record {shown}" if shown else "",
                        "numeric": reads_number(measure), "value": self._value(f"step_{n}"), "reading": self._value(f"reading_{n}"),
                        "error": " ".join(self.errors.get(f"step_{n}", [])) if self.is_bound else "",
                        "reading_error": " ".join(self.errors.get(f"reading_{n}", [])) if self.is_bound else "",
                        "focus": "step" if self.first_error == f"step_{n}" else "reading" if self.first_error == f"reading_{n}" else ""})
        return out

    def result_options(self) -> list[dict]:
        chosen = self._value("pm_result")
        return [{"value": value, "label": label, "hint": hint, "checked": value == chosen} for value, label, hint in RESULT_CHOICES]

    def repair_options(self, outcomes=(), hints: dict | None = None) -> list[dict]:
        """A repair's outcomes (slice 30): `outcomes` (completion.repair_outcomes: the ones this user may choose), each with its label and
        the line `hints` gives it (the view's: what it does to this device)."""
        chosen = self._value("repair_check") if self.is_repair else ""
        return [{"value": value, "label": RepairCheck(value).label, "hint": (hints or {}).get(value, ""), "checked": value == chosen}
                for value in outcomes]

    def inspection_options(self, hints: dict | None = None) -> list[dict]:
        """An inspection's result choices (slice 26), each with the line `hints` gives it (the view's: what it does to this device)."""
        chosen = self._value("inspection_result") if self.is_inspection else ""
        return [{"value": value, "label": label, "hint": (hints or {}).get(value, ""), "checked": value == chosen}
                for value, label in InspectionResult.choices]

    def checklist_errors(self) -> str:
        return " ".join(self.errors.get("checklist", [])) if self.is_bound else ""

    # --- what the service gets --------------------------------------------------------------------------------------------

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        kwargs = {"resolution": d["resolution"], "signature": d["signature"] or completion.checklist_signature(self.steps)}
        if self.offer_hours:
            kwargs["hours"] = d["hours"]
        if self.offer_late_reason:
            kwargs["late_reason"] = d["late_reason"]
        results = [{"result": d[f"step_{n}"], "reading": d[f"reading_{n}"]} for n in range(1, len(self.steps) + 1)]
        if self.is_pm:
            kwargs.update(pm_result=d["pm_result"], results=results,
                          open_repair=d["open_repair"] if self.offer_open_repair and d["shown_open_repair"] else True,
                          tag_out=d["tag_out"] if self.offer_tag_out and d["shown_tag_out"] else self.offer_tag_out)
        elif self.is_inspection:
            # Slice 26: no step answered is no checklist (completion's rule). A tag-out not on screen was never declined: its default
            # (on) takes a device that went into use meanwhile out of use when the inspection fails.
            kwargs.update(inspection_result=d["inspection_result"], results=results,
                          tag_out=d["tag_out"] if self.offer_tag_out and d["shown_tag_out"] else True)
        elif self.is_repair:
            # Slice 30: the steps with checked and ready for use only
            outcome = d["repair_check"].strip()
            kwargs.update(repair_check=outcome, results=results if outcome == RepairCheck.PASSED else None)
        if self.offer_reference:  # slice 31: any vendor job's report number, whatever the outcome (required with checked by the vendor)
            kwargs["checks_reference"] = d["checks_reference"]
        if self.offer_engineer:  # slice 31: who did the vendor's work (KEEP_CHOICE sends nothing: the engineer stored stays)
            kwargs.update(self.engineer)
        return kwargs

    def keep_open_kwargs(self) -> dict:
        """completion.keep_open's arguments (slice 30, "If a check fails"): the steps as typed (one left blank was not done), the
        signature, and the hold, taken from the post only when its box was on screen (else its default, on)."""
        d = self.cleaned_data
        return {"results": [{"result": d[f"step_{n}"], "reading": d[f"reading_{n}"]} for n in range(1, len(self.steps) + 1)],
                "signature": d["signature"] or completion.checklist_signature(self.steps),
                "hold": d["hold"] if d["shown_hold"] else True}

    def add_service_errors(self, error):
        """Show a service's ValidationError on the fields it names; the checklist's and anything else at the top of its section."""
        service_errors(self, error)

    def focus_first_error(self):
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so the template marks the first one."""
        late = ("late_reason",)
        names = [*(LAYOUT_REPAIR if self.is_repair else ()), *LAYOUT_VENDOR, "checklist",
                 *(f"{kind}_{n}" for n in range(1, len(self.steps) + 1) for kind in ("step", "reading")),
                 *(late if self.late_first else ()), *LAYOUT_TAIL, *(() if self.late_first else late)]
        self.first_error = next((name for name in names if name in self.errors), "")
        if self.first_error in self.fields:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[self.first_error].widget.attrs["autofocus"] = True
