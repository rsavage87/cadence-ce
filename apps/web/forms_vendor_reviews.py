"""
Input parsing for the Contracts screen's Reviews tab (slice 34, apps/web/views_vendor_reviews.py): New review's names and period, a block
of the reviewer's fields, the sign-off, the plan's result, and a committee report. The forms only read and shape what was typed (and
refuse a null character, which no database column may hold); apps.evaluations.vendor_reviews decides every rule (the names, the period,
the ratings' choices, the percentages, the tokens, the days against the sign-off and today) and its refusals come back on the field they
name, with what was typed kept. Every box is optional here: a missing value is the service's to refuse in its words.
"""
from django import forms

from apps.evaluations.models import CRITERIA, CRITERIA_FIELDS, ReportBody

from .forms_incidents import ServiceForm

NUL_WORDS = "Remove the null character: it cannot be stored."
OTHER = "other"  # New review's period choice for a day typed in the box
NAMES_SHOWN = 3  # New review's boxes for another spelling of the vendor

# The reviewer's fields, a block each (each saved on its own through vendor_reviews.update_review, only the fields the block posts).
RATINGS, REPORTED, DECISION, SUMMARY = "ratings", "reported", "decision", "summary"
BLOCK_FIELDS = {
    RATINGS: (*CRITERIA_FIELDS, "stated_in_contract"),
    REPORTED: ("uptime_target_pct", "uptime_reported_pct", "uptime_lowest_pct", "systems_below", "systems_total", "response_target_pct",
               "response_reported_pct", "vendor_report_reference"),
    DECISION: ("overall", "decision", "plan_due_on"),
    SUMMARY: ("summary",),
}
BLOCK_KEYS = tuple(BLOCK_FIELDS)
CRITERIA_LABELS = {f: label for f, label, _help in CRITERIA}


NUL = {"null_characters_not_allowed": NUL_WORDS}  # Django's own check of every text box, in these words


def _text(**kwargs) -> forms.CharField:
    return forms.CharField(required=False, error_messages=NUL, **kwargs)


def _box(label: str = "", **attrs) -> forms.CharField:
    """A text box the service parses: no length or format checked here (the service counts and words its refusal), stripped by the
    service, a null character refused."""
    return _text(label=label, strip=False, widget=forms.TextInput(attrs=attrs))


def _no_nul(values) -> list[str]:
    for value in values:
        if "\x00" in value:
            raise forms.ValidationError(NUL_WORDS)
    return list(values)


class NewReviewForm(ServiceForm):
    """New review: the vendor's names (the spellings ticked, `names`, and up to NAMES_SHOWN typed, `other_names`) and the last day of
    its 12 months (a choice, `period`: an ISO day, or OTHER for the day typed in `period_end`). `vendor` is the vendor whose spellings
    the form shows (its key or a name), never sent to the service."""
    vendor = _text()
    vendor_names = forms.Field(required=False)  # read from `names` and `other_names` (several values each)
    period = _text()
    period_end = _text()

    def clean_vendor_names(self) -> list[str]:
        getlist = getattr(self.data, "getlist", None)
        values = [*getlist("names"), *getlist("other_names")] if getlist else []
        return _no_nul([v.strip() for v in values if isinstance(v, str) and v.strip()])

    def clean_period_end(self) -> str:
        """The day the service is given: the choice's, or the day typed for OTHER (or with no choice), as typed."""
        choice = (self.cleaned_data.get("period") or "").strip()
        typed = (self.cleaned_data.get("period_end") or "").strip()
        return typed if choice in ("", OTHER) else choice

    @staticmethod
    def typed_in(data) -> dict:
        """What was typed in `data` (a post, or the query of a GET that re-draws the form), to show again: the spellings ticked, the
        boxes, the choice, the day."""
        getlist = getattr(data, "getlist", None)

        def one(name):
            value = data.get(name, "") if data is not None else ""
            return value if isinstance(value, str) else ""

        return {"names": getlist("names") if getlist else [], "other_names": getlist("other_names") if getlist else [],
                "period": one("period").strip(), "period_end": one("period_end")}


def block_form(key: str):
    """The form of one block of the reviewer's fields: a text box per field (choices, numbers, a day, a token, the summary), each
    parsed and refused by vendor_reviews.update_review."""
    fields = {f: _box() for f in BLOCK_FIELDS[key]}
    if key == SUMMARY:
        fields["summary"] = _text(strip=False, widget=forms.Textarea(attrs={"rows": 8}))
    return type(f"{key.title()}BlockForm", (ServiceForm,), fields)


class BlockForm:
    """Parse one block's post: the fields it names that were posted (a field left out of a post is never cleared), or the errors."""

    def __init__(self, key: str, data):
        self.key = key
        self.form = block_form(key)(data)
        self.posted = [f for f in BLOCK_FIELDS[key] if f in data]

    def values(self) -> dict | None:
        """{field: text} for update_review, or None when a box held a null character (self.errors says which)."""
        if not self.form.is_valid():
            return None
        return {f: self.form.cleaned_data[f] for f in self.posted}

    @property
    def errors(self) -> dict:
        return {k: list(v) for k, v in self.form.errors.items()}


class SignOffForm(ServiceForm):
    """Sign off: the figures the approver saw (the snapshot id the page was drawn with), the day it was signed, and the approver's name
    and title as they sign (saved on the review with the sign-off, or neither)."""
    snapshot_id = _text(widget=forms.HiddenInput)
    signed_on = _box("Signed off on", type="date")
    approver_name = _box("Approver's name", autocomplete="off")
    approver_title = _box("Approver's title", autocomplete="off")


class PlanForm(ServiceForm):
    """The corrective action plan's result and the day it closed; a blank result clears one entered in error."""
    plan_result = _text()
    plan_closed_on = _box("Closed on", type="date")


class ReportForm(ServiceForm):
    """One committee meeting that received the review (or several, from the tab): the committee, the day it met, its minutes'
    reference."""
    body = _text()
    meeting_on = _box("Meeting day", type="date")
    minutes_reference = _box("Minutes reference", autocomplete="off", spellcheck="false")
    reviews = forms.Field(required=False)  # the tab's batch: the reviews ticked (ids), read by the view

    def values(self) -> dict:
        d = self.cleaned_data
        return {"body": d.get("body", ""), "meeting_on": d.get("meeting_on", ""), "minutes_reference": d.get("minutes_reference", "")}


BODY_CHOICES = ReportBody.choices
