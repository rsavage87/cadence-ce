"""
Input parsing for completing a work order (slice 15, web/_wo_complete.html). The steps depend on the device's PM procedure, so
their fields (step_<n>: pass, fail, or na; reading_<n>) are built in __init__ from the checklist the modal shows. The rules live
in apps.workorders.completion; this form passes on what was typed, and puts the service's errors back on the fields they name
(the service keys them by these same names).
"""
from django import forms

from apps.workorders import completion
from apps.workorders.models import PmResult

RESOLUTION_PLACEHOLDER = "What was found and done. No patient information."
PM_RESOLUTION_PLACEHOLDER = "Optional when the PM passed. What was found, repaired, or failed. No patient information."
# The overall result's choices, each with a line saying when it fits (the modal's radio cards)
RESULT_CHOICES = [
    (PmResult.PASS, "Pass", "Every step passed or did not apply"),
    (PmResult.PASS_MINOR_REPAIR, "Pass with minor repair", "Put right during the PM; say what in the resolution"),
    (PmResult.FAIL, "Fail", "Needs a repair before it is relied on"),
]
STEP_CHOICES = [(completion.PASS, "Pass"), (completion.FAIL, "Fail"), (completion.NA, "N/A")]
# The fields after the checklist, in the modal's order: a form sent back with errors focuses the first one on screen
LAYOUT_TAIL = ("pm_result", "resolution", "open_repair")


class CompleteForm(forms.Form):
    resolution = forms.CharField(required=False, strip=False, widget=forms.Textarea(attrs={"rows": 3, "maxlength": completion.RESOLUTION_MAX}))
    pm_result = forms.CharField(required=False)
    open_repair = forms.BooleanField(required=False, initial=True)
    tag_out = forms.BooleanField(required=False, initial=True)
    signature = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, steps=(), is_pm=False, offer_open_repair=False, offer_tag_out=False, **kwargs):
        """`steps`: the checklist the modal shows, as completion.checklist_of gives it. The two offers say whether the failed-PM
        options are on screen: an option that is not offered is not taken from the post (a new repair is opened; no tag-out)."""
        kwargs.setdefault("auto_id", "wc-%s")
        super().__init__(*args, **kwargs)
        self.steps, self.is_pm = list(steps), is_pm
        self.offer_open_repair, self.offer_tag_out = offer_open_repair, offer_tag_out
        self.first_error = ""
        for n in range(1, len(self.steps) + 1):
            self.fields[f"step_{n}"] = forms.CharField(required=False)
            self.fields[f"reading_{n}"] = forms.CharField(required=False)
        self.fields["signature"].initial = completion.checklist_signature(self.steps)
        attrs = self.fields["resolution"].widget.attrs
        attrs["placeholder"] = PM_RESOLUTION_PLACEHOLDER if is_pm else RESOLUTION_PLACEHOLDER
        if not is_pm:
            attrs["autofocus"] = True

    @classmethod
    def filled(cls, params, *, fill: str = "", **kwargs) -> "CompleteForm":
        """An unbound form showing `params` (the modal's values, sent back to re-render it) with every unanswered step set to
        `fill`: "Mark the rest pass" without saving anything."""
        form = cls(**kwargs)
        initial = {name: params.get(name, "") for name in form.fields if name not in ("open_repair", "tag_out", "signature")}
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
        out = []
        for n, (text, measure) in enumerate(self.steps, 1):
            out.append({"n": n, "text": text, "measured": measure is not None, "measure": "" if measure in (None, True) else measure,
                        "value": self._value(f"step_{n}"), "reading": self._value(f"reading_{n}"),
                        "error": " ".join(self.errors.get(f"step_{n}", [])) if self.is_bound else "",
                        "reading_error": " ".join(self.errors.get(f"reading_{n}", [])) if self.is_bound else "",
                        "focus": "step" if self.first_error == f"step_{n}" else "reading" if self.first_error == f"reading_{n}" else ""})
        return out

    def result_options(self) -> list[dict]:
        chosen = self._value("pm_result")
        return [{"value": value, "label": label, "hint": hint, "checked": value == chosen} for value, label, hint in RESULT_CHOICES]

    def checklist_errors(self) -> str:
        return " ".join(self.errors.get("checklist", [])) if self.is_bound else ""

    # --- what the service gets --------------------------------------------------------------------------------------------

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        kwargs = {"resolution": d["resolution"], "signature": d["signature"] or completion.checklist_signature(self.steps)}
        if self.is_pm:
            kwargs.update(pm_result=d["pm_result"],
                          results=[{"result": d[f"step_{n}"], "reading": d[f"reading_{n}"]} for n in range(1, len(self.steps) + 1)],
                          open_repair=d["open_repair"] if self.offer_open_repair else True,
                          tag_out=d["tag_out"] if self.offer_tag_out else False)
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
        names = ["checklist", *(f"{kind}_{n}" for n in range(1, len(self.steps) + 1) for kind in ("step", "reading")), *LAYOUT_TAIL]
        self.first_error = next((name for name in names if name in self.errors), "")
        if self.first_error in self.fields:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[self.first_error].widget.attrs["autofocus"] = True
