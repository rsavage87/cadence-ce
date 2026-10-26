"""
The security advisory modals (slice 40; views_cyber_advisories.py): Record advisory, Edit advisory (the day received and the CVE IDs),
Add source and Edit source, Record revision, Set models (with "Add a version not on file"), and the confirmations (Confirm models, Not
applicable here, Close, Reopen, Entered in error, Remove model, Remove source). The rules live in apps.cybersecurity.advisories: these
forms render the fields, keep what was typed, and hand it to the writers as typed (days as ISO text, choices as their slugs, CVE IDs as
the pasted text), so every problem comes back at once in the writers' words, keyed by the field it names (the writers' keys are these
fields' names; one about the advisory or the model at the top). Set models sends only the fields its choices show (ModelForm.service_kwargs:
a field a choice hides is never refused out of sight).

Choices are set in __init__ from what the view read (the model's releases with their devices on site, the catalog's models), never a
class-level queryset. No free text: references are tokens, the link an https address the writer checks, CVE IDs a pasted list. Never
"required" (a field the writer needs says so in its refusal), never a never word (rules.DECISION_NEVER_WORDS), and every CVE example is
the format CVE-YYYY-NNNN, never a real ID.
"""
from django import forms

from apps.cybersecurity.models import (
    ADVISORY_REFERENCE_MAX,
    ADVISORY_URL_MAX,
    REVISION_MAX,
    UPDATE_REFERENCE_MAX,
    VERSION_MAX,
    AdvisoryChange,
    AdvisoryRemedy,
    AdvisorySourceKind,
    AdvisoryVersions,
)

from .forms_cybersecurity import ServiceForm

DATE = {"type": "date"}
CHOOSE = ("", "Choose…")

# Help lines (the writers word every refusal)
RECEIVED_HELP = "The day your team took it in: not after today."
CVES_HELP = ("As the advisory lists them (CVE-YYYY-NNNN), separated by spaces, commas, or new lines. Leave it blank when it lists none. "
             "Cadence keeps the IDs, never the advisory's text.")
CVES_ADD_HELP = "Any the advisory adds to the ones already recorded (CVE-YYYY-NNNN), separated by spaces, commas, or new lines."
SOURCE_HELP = "Who published it."
REFERENCE_HELP = "As its publisher prints it, e.g. ICSMA-25-030-01."
URL_HELP = "The publisher's https link to its page (a pointer: Cadence never opens it)."
PUBLISHED_HELP = "The day its publisher dated it, if printed."
REVISION_HELP = "As its publisher prints it, e.g. UpdateA or 3."
UPDATED_HELP = "The day the publisher dated the revision: not before the day published."
CHANGE_HELP = ("What the revision says changed. Products or releases changed, vulnerabilities added, or a fix named again asks to confirm "
               "the models it names.")
VERSIONS_HELP = ("The releases listed: a device on another release, or on the fix, is not named. Every release, or releases not stated: "
                 "every device of the model on site is.")
RELEASES_HELP = "The model's releases on file, each with its devices on site. A device with no release recorded is listed apart."
REMEDY_HELP = "As the advisory states it, never Cadence's judgment."
FIX_HELP = "For A release from the manufacturer: the release the advisory names as the fix, never one it lists."
PATCHES_HELP = "For the manufacturer's validated patches: the patch list the advisory names, as written, e.g. ICAR-2026-10. Blank when it names none."
CONFIRM_LABEL = "These are all the models it names that are here"
CONFIRM_HELP = "Untick it when the advisory names another model of this facility's catalog still to add."
ADD_VERSION_LABEL = "Add a version not on file"
ADD_VERSION_HELP = "As the manufacturer writes it. It is added to the model's releases at once, whether or not you save these models."


def _text(label: str, limit: int, help_text: str = "", placeholder: str = ""):
    attrs = {"autocomplete": "off", "maxlength": limit, "spellcheck": "false"}
    if placeholder:
        attrs["placeholder"] = placeholder
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.TextInput(attrs=attrs))


def _day(label: str, help_text: str = ""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs=DATE))


def _select(label: str, help_text: str = ""):
    """A choice kept as typed: the writer refuses one not listed, in its words, with the other problems."""
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.Select())


def _radios(label: str, choices, help_text: str = ""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.RadioSelect(choices=list(choices)))


def _cves(label: str, help_text: str):
    return forms.CharField(label=label, required=False, help_text=help_text, strip=False,
                           widget=forms.Textarea(attrs={"rows": 3, "spellcheck": "false", "autocomplete": "off",
                                                        "placeholder": "CVE-YYYY-NNNN CVE-YYYY-NNNN"}))


class _AnyOf(forms.MultipleChoiceField):
    """Several values kept as typed: the writer refuses one that is not a release of the model, or not a change it knows, in its
    words."""

    def valid_value(self, value) -> bool:
        return True


def _value(form, name: str) -> str:
    value = form.cleaned_data.get(name)
    return value.strip() if isinstance(value, str) else ""


def _values(form, name: str) -> list:
    return [v.strip() for v in form.cleaned_data.get(name) or [] if isinstance(v, str) and v.strip()]


class _SourceFields(ServiceForm):
    """A source's fields: who published it, its reference, its link, and the day published."""

    source = _select("Published by", SOURCE_HELP)
    reference = _text("Reference", ADVISORY_REFERENCE_MAX, REFERENCE_HELP, placeholder="e.g. ICSMA-25-030-01")
    url = _text("Link", ADVISORY_URL_MAX, URL_HELP, placeholder="https://")
    published_on = _day("Published", PUBLISHED_HELP)

    def _setup_source(self):
        self.fields["source"].widget.choices = [CHOOSE, *AdvisorySourceKind.choices]
        self.fields["url"].widget.input_type = "url"

    def source_kwargs(self) -> dict:
        return {"source": _value(self, "source"), "reference": _value(self, "reference"), "url": _value(self, "url"),
                "published_on": _value(self, "published_on") or None}


class RecordAdvisoryForm(_SourceFields):
    """Record advisory (advisories.record_advisory): the day received, the CVE IDs, and its first source."""

    received_on = _day("Received", RECEIVED_HELP)
    cves = _cves("CVE IDs", CVES_HELP)
    layout = ("received_on", "cves", "source", "reference", "url", "published_on")

    def __init__(self, *args, today, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        kwargs.setdefault("initial", {"received_on": today.isoformat()})
        super().__init__(*args, **kwargs)
        self._setup_source()

    def service_kwargs(self) -> dict:
        return {"received_on": _value(self, "received_on"), "cves": self.cleaned_data.get("cves") or "", **self.source_kwargs()}


class EditAdvisoryForm(ServiceForm):
    """Edit advisory (advisories.update_advisory): the day received and the CVE IDs, as one list."""

    received_on = _day("Received", RECEIVED_HELP)
    cves = _cves("CVE IDs", CVES_HELP)
    layout = ("received_on", "cves")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)

    def service_kwargs(self) -> dict:
        return {"received_on": _value(self, "received_on"), "cves": self.cleaned_data.get("cves") or ""}


class SourceForm(_SourceFields):
    """Add source (advisories.add_source, with the CVE IDs it adds) and Edit source (advisories.update_source: `adding` False, no CVE
    IDs)."""

    cves = _cves("CVE IDs it adds", CVES_ADD_HELP)
    layout = ("source", "reference", "url", "published_on", "cves")

    def __init__(self, *args, adding: bool, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)
        self._setup_source()
        if not adding:
            del self.fields["cves"]

    def service_kwargs(self) -> dict:
        found = self.source_kwargs()
        if "cves" in self.fields:
            found["cves"] = self.cleaned_data.get("cves") or ""
        return found


class RevisionForm(ServiceForm):
    """Record revision (advisories.record_revision): the revision as printed, the day updated, and what changed."""

    revision = _text("Revision", REVISION_MAX, REVISION_HELP, placeholder="e.g. UpdateA")
    updated_on = _day("Updated", UPDATED_HELP)
    last_change = _AnyOf(label="What changed", required=False, help_text=CHANGE_HELP, widget=forms.CheckboxSelectMultiple,
                         choices=AdvisoryChange.choices)
    layout = ("revision", "updated_on", "last_change")

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)

    def service_kwargs(self) -> dict:
        return {"revision": _value(self, "revision"), "updated_on": _value(self, "updated_on") or None,
                "last_change": _values(self, "last_change")}


class ModelForm(ServiceForm):
    """Set models for one model (advisories.set_model): which releases it names (the releases listed, with `releases`: [(release, its
    devices on site, its OS name)]), what it offers, the fix (a release of the model), the patch list, and "These are all the models it
    names that are here" (checked to begin with). "Add a version not on file" (add_version) is services.add_release's, posted on its
    own (its refusal keyed `version` lands on it; the view adds a version typed there before any save, never sending it here)."""

    versions = _radios("Releases it names", AdvisoryVersions.choices, VERSIONS_HELP)
    release_ids = _AnyOf(label="The releases it lists", required=False, help_text=RELEASES_HELP, widget=forms.CheckboxSelectMultiple)
    remedy = _radios("What the advisory offers", AdvisoryRemedy.choices, REMEDY_HELP)
    fix_release = _select("The release it names as the fix", FIX_HELP)
    patches_reference = _text("The patch list it names", UPDATE_REFERENCE_MAX, PATCHES_HELP, placeholder="e.g. ICAR-2026-10")
    confirm = forms.BooleanField(label=CONFIRM_LABEL, required=False, help_text=CONFIRM_HELP)
    add_version = _text(ADD_VERSION_LABEL, VERSION_MAX, ADD_VERSION_HELP, placeholder="e.g. 5.8")
    aliases = {"version": "add_version"}  # services.add_release's key
    layout = ("versions", "release_ids", "add_version", "remedy", "fix_release", "patches_reference", "confirm")

    def __init__(self, *args, releases, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)
        self.fields["release_ids"].choices = [(str(r.pk), _release_label(r, n, os_name)) for r, n, os_name in releases]
        self.fields["fix_release"].widget.choices = [CHOOSE, *((str(r.pk), r.version) for r, _n, _os in releases)]

    def service_kwargs(self) -> dict:
        """What the choices show is sent, nothing else (as slice 38's update forms): the releases only with The releases listed, the
        fix only with A release from the manufacturer, the patch list only with the manufacturer's validated patches. A field its choice
        hides (CSS :has) keeps what was in it when the choice moved on, and the writer's refusal of it would land where nobody sees it;
        the writer keeps those refusals for the API."""
        versions, remedy = _value(self, "versions"), _value(self, "remedy")
        return {"versions": versions, "release_ids": _values(self, "release_ids") if versions == AdvisoryVersions.LISTED else [],
                "remedy": remedy, "fix_release": (_value(self, "fix_release") or None) if remedy == AdvisoryRemedy.RELEASE else None,
                "patches_reference": _value(self, "patches_reference") if remedy == AdvisoryRemedy.PATCHES else "",
                "confirm": bool(self.cleaned_data.get("confirm"))}


def _release_label(release, on_site: int, os_name: str) -> str:
    """ "5.2 (demo) · Demo embedded OS 7 · 12 devices on site"."""
    parts = [release.version]
    if os_name:
        parts.append(os_name)
    parts.append(f"{on_site} device{'' if on_site == 1 else 's'} on site")
    return " · ".join(parts)


def model_initial(row) -> dict:
    """Set models' values for a model it names already (the row as stored), or a new one's: "These are all the models it names that
    are here" checked."""
    if row is None:
        return {"confirm": True}
    return {"versions": row.versions, "release_ids": [str(r.pk) for r in getattr(row, "listed", ())], "remedy": row.remedy,
            "fix_release": str(row.fix_release_id) if row.fix_release_id else "", "patches_reference": row.patches_reference,
            "confirm": True}


class NotApplicableForm(ServiceForm):
    """Not applicable here (advisories.mark_not_applicable): one reason, each checked against the records (the view shows each one's
    refusal before it is tried)."""

    not_applicable_reason = forms.CharField(required=False)
    layout = ("not_applicable_reason",)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)

    def reason(self) -> str:
        return _value(self, "not_applicable_reason")


class ConfirmForm(ServiceForm):
    """A confirmation (Confirm models, Close, Reopen, Entered in error, Remove model, Remove source): nothing to choose; the writer's
    refusal at the top."""

    aliases = {}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "ad-%s")
        super().__init__(*args, **kwargs)

