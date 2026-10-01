"""
Input parsing for New procedure and Edit procedure in the model drawer (slice 14). The rules (code, hours, link, checklist) live in
apps.pm.procedures; this form carries what was typed to it and puts its errors back on the fields they name. Every field is a plain
text box here, so the service's messages are the ones shown (not Django's "Enter a valid URL" or its own decimal wording).
"""
from django import forms

from apps.pm import procedures as svc
from apps.pm.models import PmProcedure

# Screen order, so a form sent back with errors focuses the first one on screen. Also apps.pm.procedures' keyword arguments.
LAYOUT = ("code", "revision", "name", "source_kind", "estimated_hours", "source_reference", "source_url", "checklist")


def plain_hours(value) -> str:
    """1.50 -> "1.5", 2.00 -> "2": the hours as a person would type them."""
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _text(label, maxlength, help_text="", **attrs):
    return forms.CharField(label=label, required=False, strip=False, help_text=help_text,
                           widget=forms.TextInput(attrs={"maxlength": maxlength, "autocomplete": "off", **attrs}))


class ProcedureForm(forms.Form):
    code = _text("Code", svc.CODE_MAX, "Letters, digits, and - . _ / (no spaces). Unique in this facility.", placeholder="e.g. HA-G5-PM6",
                 spellcheck="false", autofocus=True)
    revision = _text("Revision", svc.REVISION_MAX, placeholder="e.g. Rev K")
    name = _text("Name", svc.NAME_MAX, placeholder="e.g. ICU ventilator 6-month PM")
    source_kind = forms.ChoiceField(label="Source", choices=PmProcedure.SourceKind.choices, initial=PmProcedure.SourceKind.OEM,
                                    error_messages={"required": "Choose where the procedure comes from.",
                                                    "invalid_choice": "Choose where the procedure comes from."})
    estimated_hours = _text("Estimated hours per PM", 8, f"{svc.HOURS_MIN} to {svc.HOURS_MAX}, at most {svc.HOURS_PLACES} decimal places.",
                            placeholder="e.g. 1.5", inputmode="decimal")
    source_reference = _text("Reference", svc.REFERENCE_MAX, "The manual's title and section, or the ECRI procedure number.",
                             placeholder="e.g. G5 Service Manual, section 7")
    source_url = forms.CharField(label="Link to the source document", required=False, strip=False,
                                 widget=forms.URLInput(attrs={"maxlength": svc.URL_MAX, "placeholder": "https://", "autocomplete": "off",
                                                              "spellcheck": "false"}))
    checklist = forms.CharField(label="Checklist", required=False, strip=False, help_text=svc.LINE_FORMAT,
                                widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "true"}))

    def __init__(self, *args, procedure: PmProcedure | None = None, initial: dict | None = None, **kwargs):
        kwargs.setdefault("auto_id", "pr-%s")
        if procedure is not None and not args and not kwargs.get("data"):
            initial = {"code": procedure.code, "revision": procedure.revision, "name": procedure.name, "source_kind": procedure.source_kind,
                       "estimated_hours": plain_hours(procedure.estimated_hours), "source_reference": procedure.source_reference,
                       "source_url": procedure.source_url, "checklist": svc.checklist_text(procedure.checklist), **(initial or {})}
        super().__init__(*args, initial=initial, **kwargs)
        self.procedure = procedure

    def service_fields(self) -> dict:
        """apps.pm.procedures' keyword arguments: every field as typed (the service trims and checks)."""
        return {name: self.cleaned_data.get(name, "") for name in LAYOUT}

    def add_service_errors(self, error):
        """A service's ValidationError on the fields it names; anything else at the top."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.fields else None, messages)

    def focus_first_error(self):
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so make that the first field with an error."""
        first = next((name for name in LAYOUT if name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True
