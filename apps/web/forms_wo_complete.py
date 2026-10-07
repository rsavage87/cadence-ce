"""
Input parsing for completing a work order (slice 15, web/_wo_complete.html). The steps depend on the device's PM procedure, so
their fields (step_<n>: pass, fail, or na; reading_<n>) are built in __init__ from the checklist the modal shows. The rules live
in apps.workorders.completion; this form passes on what was typed, and puts the service's errors back on the fields they name
(the service keys them by these same names).

Slice 24, on a phone: a reading box brings up the number pad (inputmode="decimal") when its step reads a number (reads_number), and
an optional Hours box logs time with the completion (offered by the view when there is someone to log it for).

Slice 25: a PM completed after its due date (completion.completes_late) gets an optional "Why was it late?" select (late_reason,
LateReason's choices). Blank keeps a reason already recorded; the blank choice says which. The view says where it sits: before the
result for a life-support or high-risk PM (late_first), else after the hours.

Slice 26: an incoming inspection (is_inspection) takes its checklist like a PM (every step or none: completion's rule) and its result
(inspection_result, InspectionResult: required while the device waits for it, optional otherwise; the service says so). A failed
one's tag-out (a device put in use before its inspection) is taken from the post only when the modal showed it; otherwise its
default (on), which the service applies only to a device in use.
"""
import re

from django import forms

from apps.workorders import completion
from apps.workorders.models import InspectionResult, LateReason, PmResult

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


class CompleteForm(forms.Form):
    resolution = forms.CharField(required=False, strip=False, widget=forms.Textarea(attrs={"rows": 3, "maxlength": completion.RESOLUTION_MAX}))
    # Text, not a number field: apps.workorders.costs says what a valid number of hours is. inputmode brings up the number pad.
    hours = forms.CharField(required=False, widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off", "placeholder": "e.g. 1.5",
                                                                          "aria-describedby": "wc-hours-for"}))
    pm_result = forms.CharField(required=False)
    # Slice 26: text, not a choice field: completion checks the value (a refusal keyed inspection_result, in its words)
    inspection_result = forms.CharField(required=False)
    open_repair = forms.BooleanField(required=False, initial=True)
    tag_out = forms.BooleanField(required=False, initial=True)
    signature = forms.CharField(required=False, widget=forms.HiddenInput)
    # Which failed-PM options were on screen when the modal was filled in (the template posts these next to each checkbox): the
    # device can change while the modal is open, and an option not shown was never declined.
    shown_open_repair = forms.CharField(required=False)
    shown_tag_out = forms.CharField(required=False)
    # Slice 25: text, not a choice field: completion checks the value (a refusal keyed late_reason, in its words). Choices in __init__.
    late_reason = forms.CharField(required=False, widget=forms.Select(attrs={"aria-describedby": "wc-late-hint"}))

    def __init__(self, *args, steps=(), is_pm=False, is_inspection=False, kept_pass=False, offer_open_repair=False, offer_tag_out=False,
                 offer_hours=False, offer_late_reason=False, late_first=False, late_recorded="", **kwargs):
        """`steps`: the checklist the modal shows, as completion.checklist_of gives it. The two offers say whether the failed-PM
        options are on screen: an option that is not offered is not taken from the post (a new repair is opened; no tag-out).
        `offer_hours`: the Hours box is on screen; without it no hours are taken from the post. `offer_late_reason` (slice 25): the
        "Why was it late?" select is on screen (before the result when `late_first`); `late_recorded` names the reason already on
        record, which its blank choice keeps. Slice 26: `is_inspection`, an incoming inspection (its result instead of a PM's, and a
        failed one's tag-out under `offer_tag_out`); `kept_pass`, one that passed and whose device no longer waits (it stays
        passed: Passed is chosen to begin with)."""
        kwargs.setdefault("auto_id", "wc-%s")
        super().__init__(*args, **kwargs)
        self.steps, self.is_pm, self.is_inspection = list(steps), is_pm, is_inspection and not is_pm
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
        if not (is_pm or self.is_inspection):  # a PM's or an inspection's checklist comes first
            attrs["autofocus"] = True

    @classmethod
    def filled(cls, params, *, fill: str = "", **kwargs) -> "CompleteForm":
        """An unbound form showing `params` (the modal's values, sent back to re-render it) with every unanswered step set to
        `fill`: "Mark the rest pass" without saving anything."""
        form = cls(**kwargs)
        initial = {name: params.get(name, "") for name in form.fields
                   if name not in ("open_repair", "tag_out", "signature", "shown_open_repair", "shown_tag_out")}
        initial.update(open_repair="open_repair" in params, tag_out="tag_out" in params)
        if params.get("signature"):  # the checklist the values were typed against, so a revision since is still caught on save
            initial["signature"] = params["signature"]
        if fill in dict(STEP_CHOICES):
            for n in range(1, len(form.steps) + 1):
                if not initial.get(f"step_{n}"):
                    initial[f"step_{n}"] = fill
        form.initial.update(initial)
        return form

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
                        "prompt": shown if shown.lower().startswith("record ") else f"Record {shown}" if shown else "",
                        "numeric": reads_number(measure), "value": self._value(f"step_{n}"), "reading": self._value(f"reading_{n}"),
                        "error": " ".join(self.errors.get(f"step_{n}", [])) if self.is_bound else "",
                        "reading_error": " ".join(self.errors.get(f"reading_{n}", [])) if self.is_bound else "",
                        "focus": "step" if self.first_error == f"step_{n}" else "reading" if self.first_error == f"reading_{n}" else ""})
        return out

    def result_options(self) -> list[dict]:
        chosen = self._value("pm_result")
        return [{"value": value, "label": label, "hint": hint, "checked": value == chosen} for value, label, hint in RESULT_CHOICES]

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
        return kwargs

    def add_service_errors(self, error):
        """Show a service's ValidationError on the fields it names; the checklist's and anything else at the top of its section."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            if key in self.fields:
                self.add_error(key, messages)
            else:
                self._errors.setdefault(key, self.error_class()).extend(messages)  # "checklist": shown above the steps

    def focus_first_error(self):
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so the template marks the first one."""
        late = ("late_reason",)
        names = ["checklist", *(f"{kind}_{n}" for n in range(1, len(self.steps) + 1) for kind in ("step", "reading")),
                 *(late if self.late_first else ()), *LAYOUT_TAIL, *(() if self.late_first else late)]
        self.first_error = next((name for name in names if name in self.errors), "")
        if self.first_error in self.fields:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[self.first_error].widget.attrs["autofocus"] = True
