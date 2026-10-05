"""
Input parsing for the AEM tab's modals (slice 14): Propose, the committee's decision, and End AEM. The rules (an interval that
differs from the OEM and the one in force, a rationale, the committee's date and minutes, a reason to end) live in apps.pm.aem;
these forms only parse what was typed and put the service's errors back on the fields they name.
"""

from django import forms
from django.utils import timezone

from apps.pm import aem

APPROVE, REJECT = "approve", "reject"
RATIONALE_PLACEHOLDER = "The failure history, what the PMs found, and why the new interval is safe. No patient information."


class AemForm(forms.Form):
    def add_service_errors(self, error):
        """A service's ValidationError on the fields it names; anything else at the top. Never add_error(None, e) with a dict."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)


class ProposeForm(AemForm):
    # Text, not a number field: apps.pm.aem says what a valid interval is, in one place.
    interval_months = forms.CharField(label="Proposed PM interval, months", required=False,
                                      widget=forms.NumberInput(attrs={"min": 1, "max": aem.INTERVAL_MAX, "step": 1, "autofocus": True}))
    rationale = forms.CharField(label="The case for the change", required=False, max_length=aem.RATIONALE_MAX,
                                widget=forms.Textarea(attrs={"rows": 4, "placeholder": RATIONALE_PLACEHOLDER}))


class DecideForm(AemForm):
    decision = forms.ChoiceField(choices=[(APPROVE, "Approve"), (REJECT, "Reject")], widget=forms.HiddenInput)
    decided_on = forms.DateField(label="Committee meeting date", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    note = forms.CharField(label="Minutes reference", required=False, max_length=aem.NOTE_MAX,
                           widget=forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. EMC minutes, September meeting, item 4"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["decided_on"].widget.attrs["max"] = timezone.localdate().isoformat()  # the service refuses a future date; the picker can say so first

    @property
    def choice(self) -> str:
        value = self.data.get("decision") if self.is_bound else self.initial.get("decision")
        return REJECT if value == REJECT else APPROVE


class EndForm(AemForm):
    reason = forms.CharField(label="Why it ends", required=False, max_length=aem.REASON_MAX,
                             widget=forms.Textarea(attrs={"rows": 2, "autofocus": True,
                                                          "placeholder": "e.g. Repairs rose at the 24-month interval; back to the OEM schedule"}))
