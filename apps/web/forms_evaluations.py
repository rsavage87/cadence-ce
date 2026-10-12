"""
Input parsing for Reports' Program evaluation (slice 32, apps/web/views_evaluations.py): New evaluation's period, an objective, and a
committee's approval. The forms read and shape what was typed; apps.evaluations.services decides (the period's rules, the limits, the
tokens, the dates against the day it was sent), and its refusals come back on the field they name with what was typed kept. A narrative
field, the inventory review day, and the plan reviewed post their raw values to services.update_text, which parses and refuses them.
"""
from datetime import date

from django import forms

from apps.evaluations.models import (
    COMMENT_MAX,
    OBJECTIVE_MAX,
    PRIVACY_WARNING,
    TARGET_MAX,
    Body,
    ObjectiveKind,
    ObjectiveResult,
)
from apps.evaluations.services import BODY_NAME_MAX, CHAIR_MAX, MINUTES_MAX

from .forms_incidents import ServiceForm

DATE = {"type": "date"}
MONTH_WORDS = "Choose the month the period starts."
NOT_ENDED = "The 12 months from {day} have not ended: an evaluation covers 12 months that have ended. Choose an earlier month."


def parse_month(value) -> date | None:
    """The first day of the month typed: "2025-10" (a month box), or "2025-10-01" (a browser without one, or a typed day: the service
    refuses a day other than the first). None for anything else."""
    text = str(value or "").strip()
    try:
        if len(text) == 7:
            year, month = text.split("-")
            return date(int(year), int(month), 1)
        return date.fromisoformat(text)
    except ValueError:
        return None


def month_value(d: date | None) -> str:
    return f"{d:%Y-%m}" if d else ""


class NewEvaluationForm(ServiceForm):
    """New evaluation: the 12 months it covers, by their first month."""
    start = forms.CharField(label="First month of the 12", required=False,
                            widget=forms.TextInput(attrs={"type": "month", "placeholder": "YYYY-MM", "autocomplete": "off"}))

    def __init__(self, *args, today: date, **kwargs):
        self.today = today
        super().__init__(*args, **kwargs)

    def clean_start(self) -> date:
        start = parse_month(self.cleaned_data.get("start"))
        if start is None:
            raise forms.ValidationError(MONTH_WORDS)
        # A start after this month can never have ended (and a year such as 9999 has no end Cadence can count to): refused here in the
        # service's words, before the service is asked.
        if (start.year, start.month) > (self.today.year, self.today.month):
            raise forms.ValidationError(NOT_ENDED.format(day=f"{start:%b} {start.day}, {start.year}"))
        return start


def _text(label, limit, *, required=False, textarea=False, help_text=""):
    """A text box with its limit in words beside it. No max_length (nor the browser's maxlength, which cuts a paste short without a
    word): the service counts and refuses in its words, with what was typed kept."""
    widget = forms.Textarea(attrs={"rows": 3}) if textarea else forms.TextInput()
    return forms.CharField(label=label, required=required, widget=widget, help_text=help_text, strip=False)


class ObjectiveForm(ServiceForm):
    """An objective: this period's (its text, target, result, and comment) or the next period's (its text and target)."""
    text = _text("Objective", OBJECTIVE_MAX, textarea=True)
    target = _text("Target", TARGET_MAX, help_text="How you will know it was met, in a few words (optional).")
    result = forms.ChoiceField(label="Result", required=False, choices=[("", "Not recorded yet"), *ObjectiveResult.choices],
                               error_messages={"invalid_choice": "Choose met, partly met, not met, or carried forward."})
    comment = _text("Comment", COMMENT_MAX, textarea=True, help_text=PRIVACY_WARNING)

    def __init__(self, *args, kind: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        if kind != ObjectiveKind.REVIEW:
            del self.fields["result"]
            del self.fields["comment"]

    def values(self) -> dict:
        d = self.cleaned_data
        out = {"text": d.get("text", ""), "target": d.get("target", "")}
        if self.kind == ObjectiveKind.REVIEW:
            out.update(result=d.get("result", ""), comment=d.get("comment", ""))
        return out


class ApprovalForm(ServiceForm):
    """A committee's approval as its minutes record it."""
    body = forms.ChoiceField(label="Committee", choices=[("", "Choose the committee"), *Body.choices], required=False,
                             error_messages={"invalid_choice": "Choose the committee that approved it."})
    body_name = _text("Its name", BODY_NAME_MAX, help_text="For another committee: its name as the minutes give it.")
    meeting_on = forms.CharField(label="Meeting day", required=False, widget=forms.TextInput(attrs=DATE))
    chair_name = _text("Chair's name", CHAIR_MAX)
    chair_title = _text("Chair's title", CHAIR_MAX)
    minutes_reference = _text("Minutes reference", MINUTES_MAX, help_text="As your committee files them, without spaces (EOC-2026-02).")

    def clean_meeting_on(self):
        text = (self.cleaned_data.get("meeting_on") or "").strip()
        if not text:
            raise forms.ValidationError("Enter the day the committee met.")
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise forms.ValidationError("Enter the day the committee met as a date.") from None

    def values(self) -> dict:
        d = self.cleaned_data
        return {"body": d.get("body", ""), "body_name": d.get("body_name", ""), "meeting_on": d.get("meeting_on"),
                "chair_name": d.get("chair_name", ""), "chair_title": d.get("chair_title", ""), "minutes_reference": d.get("minutes_reference", "")}
