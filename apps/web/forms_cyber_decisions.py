"""
The security decision modals (slice 39; views_cyber_decisions.py): Record decision (from a release row of the model panel, the device
drawer's Security tab, the Decisions tab's Needs attention, or a decision's Review and renew), End, and Entered in error. The rules live
in apps.cybersecurity.decisions and rules: these forms render the fields, keep what was chosen, and hand it to the service as typed
(days as ISO text, choices as their slugs, the plan owner as an id), so every problem comes back at once in the service's words, keyed by
the field it names (the service's keys are these fields' names; one about the scope at the top).

- The scope is fixed by the door (the modal's address), never a field. The reasons are what holds now for the scope's devices (the view
  reads them through rules.support_need), shown as words and sent as hidden values: a change meanwhile is refused under them
  (decisions.REASONS_NOW) and the modal comes back with the reasons as they hold then.
- The controls are rules.SUPPORT_CONTROLS as checkboxes with their words (never network_removed: rules.NETWORK_REMOVED_HINT says to record
  the device as not connected instead); IT's reference with rules.CONTROLS_REFERENCE_HELP.
- The plan, its day, and its owner (an active, unscoped person of this facility with Device security View: the view reads them); "Return
  it to its owner" only on one rental's, vendor loaner's, or demo unit's own decision (Review and renew of one whose device the facility
  has since kept starts at No plan recorded: a plan not offered is never prefilled).
- The review day: blank for the default (rules.review_default, worded by rules.review_rule_words for each response). A day already
  passed (an exception that lapsed, decided in the past) is said once before it is saved (decisions.REVIEW_PASSED), only when the
  service would take the decision (the view's dry run; else its refusals come back first); the form sends it again with `review_passed`
  naming that day.

Choices are set in __init__ from what the view read, never a class-level queryset. Never "required" (a field the service needs says so
in its refusal: "Choose ...", "Enter ..."), never a never word (rules.DECISION_NEVER_WORDS).
"""
from django import forms

from apps.cybersecurity import rules
from apps.cybersecurity.models import EXCEPTION_REFERENCE_MAX, AcceptedBy, DecisionPlan, DecisionResponse

from .forms_cybersecurity import ServiceForm

DATE = {"type": "date"}
CHOOSE = ("", "Choose…")
NO_OWNER = ("", "Nobody named")

CONTROLS_HELP = "What is in place for these devices now, as IT and clinical engineering state it. Choose them for Keep in use with controls only."
REFERENCE_PLACEHOLDER = "e.g. CHG0012345"
EXCEPTION_HELP = ("IT's GRC, exception, or ticket number for this decision, if it has one. IT's exception record stays the system of "
                  "record: Cadence keeps its number only.")
PLAN_HELP = "How the facility means to resolve it, apart from the decision itself."
PLAN_BY_HELP = "For an update or a replacement planned: the day it is planned by. A replacement reads as its fiscal year."
OWNER_HELP = "Optional, with a plan: who carries it out (an active person here with Device security View)."
NO_OWNERS = "Nobody here holds Device security View yet: leave it blank."
ACCEPTED_HELP = "Who accepted the decision (or decided to take the devices out of use), as the person recording it states it."
DECIDED_HELP = "The day it was decided: not after today."
REVIEW_BLANK = "Blank for the default."  # then each response's rule (rules.review_rule_words)


def _text(label: str, help_text: str = "", placeholder: str = ""):
    attrs = {"autocomplete": "off", "maxlength": EXCEPTION_REFERENCE_MAX, "spellcheck": "false"}
    if placeholder:
        attrs["placeholder"] = placeholder
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.TextInput(attrs=attrs))


def _day(label: str, help_text: str = ""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs=DATE))


def _select(label: str, help_text: str = ""):
    """A choice kept as typed: the service refuses one not listed, in its words, with the other problems."""
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.Select())


class _AnyOf(forms.MultipleChoiceField):
    """Several values kept as typed: the service refuses one not in its list (decisions.CONTROLS_WORDS, REASONS_WORDS), in its words."""

    def valid_value(self, value) -> bool:
        return True


def _value(form, name: str) -> str:
    value = form.cleaned_data.get(name)
    return value.strip() if isinstance(value, str) else ""


def review_help(today, *, asset=None) -> str:
    """The review day's help: blank for the default (rules.review_default, its day for a decision made today), and each response's rule
    (rules.review_rule_words): "Blank for the default. Keep in use: Oct 21, 2027 if decided today (by default 12 months after the day
    decided, at most 36 months). Take out of use: Jan 21, 2027 if decided today (by default 3 months after the day decided, at most 12
    months)." A temporary device's rule names the day it is due back."""
    parts = []
    for label, response in (("Keep in use", DecisionResponse.KEEP_WITH_CONTROLS), ("Take out of use", DecisionResponse.TAKE_OUT_OF_USE)):
        rule = rules.review_rule_words(response, asset=asset)
        day = rules.day_words(rules.review_default(response, today, asset=asset))
        parts.append(f"{label}: {day} if decided today ({rule[:1].lower()}{rule[1:]}).")
    return " ".join([REVIEW_BLANK, *parts])


def owner_choices(owners) -> list:
    """[(id, name)] of the people who may own a plan (the view reads them: active, unscoped, with Device security View)."""
    return [(str(u.pk), str(u)) for u in owners]


class RecordDecisionForm(ServiceForm):
    """Record decision (decisions.record_decision): the response, the controls and IT's reference, the plan with its day and owner, who
    accepted it, IT's exception number, the day decided, and the review day. The reasons are hidden values the view writes from what holds
    now (`reasons`: the field only carries the service's refusal back to its place)."""

    reasons = _AnyOf(label="Reasons", required=False, widget=forms.MultipleHiddenInput)
    response = forms.CharField(label="Decision", required=False,
                               widget=forms.RadioSelect(choices=list(DecisionResponse.choices)))
    controls = _AnyOf(label="Controls", required=False, help_text=CONTROLS_HELP, widget=forms.CheckboxSelectMultiple)
    controls_reference = _text("IT's reference for the controls", rules.CONTROLS_REFERENCE_HELP, REFERENCE_PLACEHOLDER)
    plan = _select("Plan", PLAN_HELP)
    plan_by = _day("Planned by", PLAN_BY_HELP)
    plan_owner = _select("Plan owner", OWNER_HELP)
    accepted_by = _select("Accepted by", ACCEPTED_HELP)
    exception_reference = _text("IT exception number", EXCEPTION_HELP, "e.g. EX-1042")
    decided_on = _day("Decided on", DECIDED_HELP)
    review_by = _day("Review by")
    review_passed = forms.CharField(required=False, widget=forms.HiddenInput)
    layout = ("reasons", "response", "controls", "controls_reference", "plan", "plan_by", "plan_owner", "accepted_by", "exception_reference",
              "decided_on", "review_by")

    def __init__(self, *args, owners, today, temporary: bool = False, asset=None, **kwargs):
        """`owners`: the people who may own a plan; `temporary`: the scope is one rental's, vendor loaner's, or demo unit's own (Return it
        to its owner is offered); `asset`: that device, for the review day's words."""
        kwargs.setdefault("auto_id", "sd-%s")
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("decided_on", today.isoformat())
        initial.setdefault("plan", DecisionPlan.NONE)
        plans = [(p.value, p.label) for p in DecisionPlan if temporary or p != DecisionPlan.RETURN_TO_OWNER]
        if initial["plan"] not in {value for value, _label in plans}:
            # A plan no longer offered (Review and renew of a rental's "Return it to its owner" once the facility kept it): No plan
            # recorded, the select showing what it sends, never its first option chosen by nobody; no plan names no day and no owner.
            initial.update(plan=DecisionPlan.NONE, plan_by="", plan_owner="")
        super().__init__(*args, **kwargs)
        self.fields["controls"].choices = list(rules.SUPPORT_CONTROLS.items())
        self.fields["plan"].widget.choices = plans
        people = owner_choices(owners)
        self.fields["plan_owner"].widget.choices = [NO_OWNER, *people]
        if not people:
            self.fields["plan_owner"].help_text = NO_OWNERS
        self.fields["accepted_by"].widget.choices = [CHOOSE, *AcceptedBy.choices]
        self.fields["review_by"].help_text = review_help(today, asset=asset)

    def service_kwargs(self) -> dict:
        """record_decision's fields as typed (the scope, the reasons, and the decision superseded are the view's)."""
        d = self.cleaned_data
        return {"response": _value(self, "response"),
                "controls": [v.strip() for v in d.get("controls") or [] if isinstance(v, str) and v.strip()],
                "controls_reference": _value(self, "controls_reference"), "plan": _value(self, "plan"), "plan_by": _value(self, "plan_by"),
                "plan_owner": _value(self, "plan_owner") or None, "accepted_by": _value(self, "accepted_by"),
                "exception_reference": _value(self, "exception_reference"), "decided_on": _value(self, "decided_on"),
                "review_by": _value(self, "review_by")}


def renew_initial(decision, today) -> dict:
    """Review and renew's starting values: the decision's own (response, controls and IT's reference, plan, its day and owner, who
    accepted it), decided today, the review day blank (the default), and IT's exception number blank (a renewal is IT's to number)."""
    return {"response": decision.response, "controls": list(decision.controls or ()), "controls_reference": decision.controls_reference,
            "plan": decision.plan, "plan_by": decision.plan_by.isoformat() if decision.plan_by else "",
            "plan_owner": str(decision.plan_owner_id) if decision.plan_owner_id else "", "accepted_by": decision.accepted_by,
            "exception_reference": "", "decided_on": today.isoformat()}


class EndForm(ServiceForm):
    """End (decisions.end_decision) and Entered in error (decisions.mark_entered_in_error): a confirmation, nothing to choose; the service's
    refusal (already ended, a lock conflict) at the top."""
