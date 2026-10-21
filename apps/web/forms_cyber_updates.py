"""
The software update modals (slice 38; views_cyber_updates.py): the device drawer's Security tab's Open software update, Record update,
Confirm version, and Entered in error, and the model panel's Open updates to this release (the batch). The rules live in
apps.cybersecurity.updates: these forms render the fields, keep what was chosen, and hand it to the services as typed (ids and days as
text, choices as their slugs), so every problem comes back at once in the services' words, keyed by the field it names (the service's
keys are these fields' names; one about the device, the update, or the release at the top).

Choices are set in __init__ from what the view read (the model's releases, the device's recorded release, the technicians), never a
class-level queryset. Only what the chosen outcome or target uses is sent: a release target names no patch list and a patches target no
release; the release is for Release installed and Already on the update only, so patches applied send none, whatever the select shows
(review round 4: every door, the completion modal's too, forms_wo_complete; the service keeps the version as the device's record names
it, or says the version is not recorded, and refuses patches that name a release); "already" names no basis (updates.NO_BASIS
otherwise). The release's help says so (RELEASE_HELP; RECORD_RELEASE_HELP on Record update, which offers no Already).

The release is a statement, never a default left in place (review round 3): Record update's "Release it runs now" starts on "Choose…"
(the recorded release is marked, never chosen), and Confirm version's "Release it ran" (on the day the work order was completed, review
round 4) starts on the target release of a release target, else on "Choose…". A Release installed sent with the select untouched names
no release, and the service refuses it (CHOOSE_RUNS_NOW). The marks "(the device's record now)" are information, never a constraint.

Never "required" (a field the service needs says so in its refusal: "Choose ...", "Enter ..."), never "Critical" (the priority is normal
or high: updates.OPEN_PRIORITIES), never a never word (rules.NEVER_WORDS).
"""
from datetime import timedelta

from django import forms

from apps.cybersecurity import updates
from apps.cybersecurity.models import (
    INSTALL_OUTCOMES,
    RECORDED_OUTCOMES,
    UPDATE_REFERENCE_MAX,
    InstalledBy,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
    UpdateTarget,
)
from apps.cybersecurity.updates import UpdateChoice
from apps.workorders import permissions as wo_perms
from apps.workorders.models import Priority

from .forms_cybersecurity import ServiceForm

DATE = {"type": "date"}
CHOOSE = ("", "Choose…")
DUE_DAYS = 30  # an update's due day by default: today + 30
PRIORITY_CHOICES = [(value, Priority(value).label) for value in updates.OPEN_PRIORITIES]  # normal or high, never Critical
RECORD_OUTCOMES = INSTALL_OUTCOMES  # an install recorded by hand: installed or patches applied
CONFIRM_OUTCOMES = RECORDED_OUTCOMES  # installed, patches applied, or already on the update
LEAVE_UNASSIGNED = ("", "Leave unassigned")

# Help lines (the services word every refusal)
TARGET_HELP = "Drug libraries are not recorded here: only the device's software release or the manufacturer's patches."
TARGET_RELEASE_HELP = "The release to install, as the manufacturer writes it. The one the device's record names now is left out."
NO_OTHER_RELEASE = ("No other release of {model} is on file: add it on the Device security screen's Models tab, or choose the "
                    "manufacturer's patches.")
TARGET_REFERENCE_HELP = "For the manufacturer's patches: its patch list and its month or revision, as written, e.g. ICAR-2026-09."
DUE_HELP = "From today. The work order's due day."
RECORDED_MARK = " (the device's record now)"
# The release is read for Release installed and Already on the update only (review round 4): patches applied send none
RELEASE_HELP = "For Release installed or Already on the update. Patches applied keep the version as recorded."
RECORD_RELEASE_HELP = "For Release installed. Patches applied keep the version as recorded."  # Record update offers no Already
# Confirm version: the release on the day the work order was completed (its record may have moved on since)
CONFIRM_RELEASE_LABEL = "Release it ran"
CONFIRM_RELEASE_HELP = "On the day the work order was completed. " + RELEASE_HELP
NO_RELEASE_ON_FILE = "No release of {model} is on file yet: add it on the Device security screen's Models tab."
REFERENCE_HELP = ("For patches applied: the manufacturer's patch list and its month or revision (e.g. ICAR-2026-09). Otherwise a service "
                  "notice or field change number, if any.")
BASIS_HELP = ("For a release installed or patches applied, as the person recording it states it. The validated patch list is for "
              "patches applied only; Already on the update names none.")
INSTALLED_BY_HELP = "Who installed it: an install with no work order."
APPLIED_HELP = "The day it was installed: not after today, not before the device was installed."
ASSIGN_HELP = "A technician credentialed for this model, or nobody yet: a manager assigns it later, or a technician takes it."
NO_QUALIFIED = "No active technician holds a credential for this model: leave it unassigned, or assign it from the work order."
# Where an update goes when it is not assigned to a technician here (the service's rule, updates.open_update's assignment)
# One device's update, by a user who may not assign: in the singular, as the toast the same open gives says it (views_cyber_updates'
# LEFT_ONE and THEN_ONE); the batch keeps the service's plural (updates.ASSIGN_NEEDS)
ASSIGN_NEEDS_ONE = "Left unassigned: assigning needs Work orders Approve, so a manager assigns it or a technician takes it."
OWNER_ROUTE = "It goes to its owner, {vendor}: a {kind}'s owner installs its updates."
VENDOR_ROUTE = "It goes to vendor service ({vendor}): updates to this model are installed here by the manufacturer or its service."
BATCH_VENDOR_ROUTE = "Each goes to vendor service: updates to this model are installed here by the manufacturer or its service."
BATCH_OWNER_ROUTE = "A rental, vendor loaner, or demo unit among them goes to its owner, who installs its updates."
FROM_HELP = "The devices of this model on site whose record names one of these releases."
NOT_RECORDED_LABEL = "Release not recorded ({count})"
DEPARTMENT_HELP = "Every department, or one department's devices."
DUE_AT_NEXT_PM = "Due on each device's next PM date instead (never before today); a device with none is due on the day above."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def on_site_words(n: int) -> str:
    """ "3 devices on site": a release's devices in the batch's choices."""
    return f"{_plural(n, 'device')} on site"


def _release_choices(releases, recorded=None) -> list:
    """[(id, version)] of `releases` (the model's versioned releases, not entered in error), the recorded one marked."""
    return [(str(r.pk), f"{r.version}{RECORDED_MARK}" if recorded is not None and r.pk == recorded.pk else r.version) for r in releases]


def _text(label: str, help_text: str = "", placeholder: str = ""):
    attrs = {"autocomplete": "off", "maxlength": UPDATE_REFERENCE_MAX, "spellcheck": "false"}
    if placeholder:
        attrs["placeholder"] = placeholder
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.TextInput(attrs=attrs))


def _day(label: str, help_text: str = ""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs=DATE))


def _select(label: str, help_text: str = ""):
    """A choice kept as typed: the service refuses one not listed, in its words, with the other problems."""
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.Select())


def _radios(label: str, choices, help_text: str = ""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.RadioSelect(choices=list(choices)))


def _value(form, name: str) -> str:
    value = form.cleaned_data.get(name)
    return value.strip() if isinstance(value, str) else ""


def basis_choices(outcomes) -> list:
    """The bases the outcomes offered allow (updates.BASES), in UpdateBasis' order: the validated patch list is patches applied's only
    (the service refuses it for a release installed, in its words); already on the update names none."""
    allowed = {b for outcome in outcomes for b in updates.BASES[outcome]}
    return [(b.value, b.label) for b in UpdateBasis if b in allowed]


def technician_choices(qualified) -> list:
    """Assign to's choices: "Leave unassigned", then the technicians credentialed for the model (credentials.services.
    qualified_technicians: (technician, qualification), best match first), each with what qualifies them."""
    return [LEAVE_UNASSIGNED, *((str(t.pk), f"{t.name} ({q.via}{', expiring' if q.expiring else ''})") for t, q in qualified)]


def routing(user, updates_by: str, *, vendor: str = "", temporary_kind: str = "") -> tuple[bool, str]:
    """(whether Assign to is offered, the line saying where the one device's update goes) by open_update's rule: nobody unless the user
    may assign (updates.ASSIGN_NEEDS, worded for one work order: ASSIGN_NEEDS_ONE); a temporary device to its owner; the manufacturer's
    model to vendor service; IT or the department to nobody (updates.INSTALLED_HERE); clinical engineering or not recorded: Assign to."""
    if not wo_perms.can_assign(user):
        return False, ASSIGN_NEEDS_ONE
    if temporary_kind:
        return False, OWNER_ROUTE.format(vendor=vendor, kind=temporary_kind)
    if updates_by == UpdatesBy.MANUFACTURER:
        return False, VENDOR_ROUTE.format(vendor=vendor)
    if updates_by in (UpdatesBy.IT, UpdatesBy.DEPARTMENT):
        return False, updates.INSTALLED_HERE.format(who=updates._lower_first(UpdatesBy(updates_by).label))
    return True, ""


def batch_routing(user, updates_by: str, *, temporary: int) -> tuple[bool, list]:
    """The batch's (whether Assign to is offered, its routing lines): routing's, the manufacturer's model in the plural, and a line for
    the temporary devices among them (each to its owner) when the user may assign. A user who may not assign: the service's plural
    (updates.ASSIGN_NEEDS), never routing's one-device line."""
    if not wo_perms.can_assign(user):
        return False, [updates.ASSIGN_NEEDS]
    lines = []
    offered = False
    if updates_by == UpdatesBy.MANUFACTURER:
        lines.append(BATCH_VENDOR_ROUTE)
    else:
        offered, line = routing(user, updates_by)
        if line:
            lines.append(line)
    if temporary:
        lines.append(BATCH_OWNER_ROUTE)
    return offered, lines


class _UpdateForm(ServiceForm):
    """The outcome forms' shared part: the release the device runs (the model's releases, the recorded one marked), the reference, and
    the basis, sent as an UpdateChoice for the outcome chosen."""

    outcomes: tuple = ()

    def _setup(self, *, releases, recorded, model_name: str, initial_release=None):
        """The release choices (the recorded one marked), the bases the outcomes allow, and the release to begin with: `initial_release`,
        or none ("Choose…")."""
        self.fields["software_release"].widget.choices = [CHOOSE, *_release_choices(releases, recorded)]
        if not releases:
            self.fields["software_release"].help_text = NO_RELEASE_ON_FILE.format(model=model_name)
        self.fields["software_basis"].widget.choices = [CHOOSE, *basis_choices(self.outcomes)]
        if not self.is_bound and initial_release is not None:
            self.initial.setdefault("software_release", str(initial_release.pk))

    def choice(self) -> UpdateChoice:
        """What was chosen, as the service reads it: patches send no release, whatever the select shows (the release is for Release
        installed and Already on the update only: the version stays as the device's record names it, or is not recorded), already no
        basis."""
        outcome = _value(self, "software_outcome")
        release = _value(self, "software_release") or None
        basis = _value(self, "software_basis")
        if outcome == UpdateOutcome.PATCHES:
            release = None
        if outcome == UpdateOutcome.ALREADY:
            basis = ""
        return UpdateChoice(outcome=outcome, release_id=release, reference=_value(self, "software_reference"), basis=basis)


class RecordUpdateForm(_UpdateForm):
    """Record update (updates.record): an install with no work order: installed or patches applied, the release it runs now, the
    reference, the basis, who installed it, and the day. No release is chosen to begin with (the recorded one is only marked): a
    Release installed names the one installed, or is refused (CHOOSE_RUNS_NOW)."""

    outcomes = RECORD_OUTCOMES
    software_outcome = _radios("What was done", ((o.value, o.label) for o in RECORD_OUTCOMES))
    software_release = _select("Release it runs now", RECORD_RELEASE_HELP)
    software_reference = _text("Reference", REFERENCE_HELP, placeholder="e.g. ICAR-2026-09")
    software_basis = _select("Basis for this update", BASIS_HELP)
    installed_by = _select("Installed by", INSTALLED_BY_HELP)
    applied_on = _day("Installed on", APPLIED_HELP)
    layout = ("software_outcome", "software_release", "software_reference", "software_basis", "installed_by", "applied_on")

    def __init__(self, *args, releases, recorded, model_name: str, today, **kwargs):
        kwargs.setdefault("auto_id", "cu-%s")
        kwargs.setdefault("initial", {"applied_on": today.isoformat()})
        super().__init__(*args, **kwargs)
        self._setup(releases=releases, recorded=recorded, model_name=model_name)
        self.fields["installed_by"].widget.choices = [CHOOSE, *InstalledBy.choices]

    def service_kwargs(self) -> dict:
        return {"installed_by": _value(self, "installed_by"), "applied_on": _value(self, "applied_on")}


class ConfirmForm(_UpdateForm):
    """Confirm version (updates.confirm): what a completed update work order did, when its completion did not record it: installed,
    patches applied, or already on the update, with the release it ran on the day the work order was completed (CONFIRM_RELEASE_LABEL),
    the reference, and the basis. The release to begin with is the target release of a release target (a release of the model on
    file), else none: never the recorded one."""

    outcomes = CONFIRM_OUTCOMES
    software_outcome = _radios("What this update did", ((o.value, o.label) for o in CONFIRM_OUTCOMES))
    software_release = _select(CONFIRM_RELEASE_LABEL, CONFIRM_RELEASE_HELP)
    software_reference = _text("Reference", REFERENCE_HELP, placeholder="e.g. ICAR-2026-09")
    software_basis = _select("Basis for this update", BASIS_HELP)
    layout = ("software_outcome", "software_release", "software_reference", "software_basis")

    def __init__(self, *args, releases, recorded, target_release, model_name: str, **kwargs):
        kwargs.setdefault("auto_id", "cu-%s")
        super().__init__(*args, **kwargs)
        shown = target_release if target_release is not None and target_release in releases else None
        self._setup(releases=releases, recorded=recorded, model_name=model_name, initial_release=shown)


class RemoveForm(ServiceForm):
    """Entered in error (updates.remove): a confirmation, nothing to choose; the service's refusal (nothing recorded, a lock conflict)
    at the top."""


class OpenUpdateForm(ServiceForm):
    """Open software update (updates.open_update): the target (a release of the model other than the recorded one, or the
    manufacturer's patches with their reference), the due day, the priority (normal or high), and, where the user may assign and
    clinical engineering installs the model's updates (or nobody is recorded), Assign to."""

    target = _radios("What to install", UpdateTarget.choices, TARGET_HELP)
    target_release = _select("Release to install", TARGET_RELEASE_HELP)
    target_reference = _text("The manufacturer's patches", TARGET_REFERENCE_HELP, placeholder="e.g. ICAR-2026-09")
    due_on = _day("Due", DUE_HELP)
    priority = _select("Priority")
    assign_to = _select("Assign to", ASSIGN_HELP)
    layout = ("target", "target_release", "target_reference", "due_on", "priority", "assign_to")

    def __init__(self, *args, releases, recorded, model_name: str, today, assign: bool, qualified=(), **kwargs):
        kwargs.setdefault("auto_id", "cu-%s")
        offered = [r for r in releases if recorded is None or r.pk != recorded.pk]
        kwargs.setdefault("initial", {"target": UpdateTarget.RELEASE if offered else UpdateTarget.PATCHES,
                                      "due_on": (today + timedelta(days=DUE_DAYS)).isoformat(), "priority": Priority.NORMAL})
        super().__init__(*args, **kwargs)
        self.fields["target_release"].widget.choices = [CHOOSE, *_release_choices(offered)]
        if not offered:
            self.fields["target_release"].help_text = NO_OTHER_RELEASE.format(model=model_name)
        self.fields["priority"].widget.choices = PRIORITY_CHOICES
        if assign:
            self.fields["assign_to"].widget.choices = technician_choices(qualified)
            if not qualified:
                self.fields["assign_to"].help_text = NO_QUALIFIED
        else:
            del self.fields["assign_to"]

    def service_kwargs(self) -> dict:
        """open_update's arguments: a release target sends no reference, a patches target no release."""
        target = _value(self, "target")
        release = _value(self, "target_release") or None
        reference = _value(self, "target_reference")
        if target == UpdateTarget.RELEASE:
            reference = ""
        elif target == UpdateTarget.PATCHES:
            release = None
        return {"target": target, "target_release": release, "target_reference": reference, "due_on": _value(self, "due_on"),
                "priority": _value(self, "priority"),
                "assign_to": (_value(self, "assign_to") or None) if "assign_to" in self.fields else None}


class _AnyOf(forms.MultipleChoiceField):
    """Several values kept as typed: the service refuses one that is not a release of the model (updates.CHOOSE_FROM), in its words."""

    def valid_value(self, value) -> bool:
        return True


class OpenUpdatesForm(ServiceForm):
    """Open updates to this release (updates.open_updates): the devices of the model on site whose record names one of the releases
    chosen (each with its device count), or none ("Release not recorded"), optionally one department's; due on a day, or on each
    device's next PM; the priority; Assign to as Open software update offers it."""

    from_releases = _AnyOf(label="Update the devices on", required=False, help_text=FROM_HELP, widget=forms.CheckboxSelectMultiple)
    include_not_recorded = forms.BooleanField(label="Release not recorded", required=False)
    department = _select("Devices", DEPARTMENT_HELP)
    due_on = _day("Due", DUE_HELP)
    due_at_next_pm = forms.BooleanField(label=DUE_AT_NEXT_PM, required=False)
    priority = _select("Priority")
    assign_to = _select("Assign to", ASSIGN_HELP)
    layout = ("from_releases", "include_not_recorded", "department", "due_on", "due_at_next_pm", "priority", "assign_to")

    def __init__(self, *args, others, not_recorded: int, departments, today, assign: bool, qualified=(), **kwargs):
        """`others`: [(release, devices on site on it)] of the model's releases but the target; `departments`: [(id, name)]."""
        kwargs.setdefault("auto_id", "cu-%s")
        kwargs.setdefault("initial", {"due_on": (today + timedelta(days=DUE_DAYS)).isoformat(), "priority": Priority.NORMAL})
        super().__init__(*args, **kwargs)
        self.fields["from_releases"].choices = [(str(r.pk), f"{r.version} ({on_site_words(n)})") for r, n in others]
        self.fields["include_not_recorded"].label = NOT_RECORDED_LABEL.format(count=on_site_words(not_recorded))
        self.fields["department"].widget.choices = [("", "Every department"), *((d, f"{name} only") for d, name in departments)]
        self.fields["priority"].widget.choices = PRIORITY_CHOICES
        if assign:
            self.fields["assign_to"].widget.choices = technician_choices(qualified)
            if not qualified:
                self.fields["assign_to"].help_text = NO_QUALIFIED
        else:
            del self.fields["assign_to"]

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        return {"from_releases": [v.strip() for v in d.get("from_releases") or [] if isinstance(v, str) and v.strip()],
                "include_not_recorded": bool(d.get("include_not_recorded")), "department": _value(self, "department") or None,
                "due_on": _value(self, "due_on"), "due_at_next_pm": bool(d.get("due_at_next_pm")), "priority": _value(self, "priority"),
                "assign_to": (_value(self, "assign_to") or None) if "assign_to" in self.fields else None}

