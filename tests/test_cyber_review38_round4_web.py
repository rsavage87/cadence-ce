"""
Slice 38 review fixes, round 4 (final), the web doors (I2), on SQLite and PostgreSQL. Round 4 stops judging an outcome against the
device's register record (apps.cybersecurity.updates, I1): the doors follow.

- The release select is for Release installed and Already on the update only. Every door (the completion modal, Confirm version, Record
  update) sends no release with Patches applied, whatever the select shows (forms_wo_complete.CompleteForm._software,
  forms_cyber_updates._UpdateForm.choice), so the service never refuses it; the release's help says so (forms_cyber_updates.RELEASE_HELP,
  RECORD_RELEASE_HELP on Record update, views_wo_release.RELEASE_HINT).
- The hints: Release installed "…; the device's record moves to it from that day.", Already "It already ran the update when checked;
  nothing installed." No hint or help names the record as a constraint ("its record says", "recorded now", "the recorded one"); the
  marks on the options ("· recorded now", "(the device's record now)") stay as information.
- Confirm version's release is "Release it ran", "On the day the work order was completed".
- The skeptics' web scenarios (skeptics38c.json, web) now give a truthful result or a plain refusal: patches posted as rendered on a
  release target, a re-completion after a mistaken install (patches or already as rendered), Confirm version after Entered in error,
  Record update after a hand row entered in error, and the Installed hint when the record already named the release.
"""
import json
import re
from datetime import timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, release, update_wo
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import DeviceSecurity, SoftwareUpdate, UpdateBasis, UpdateOutcome, UpdatesBy
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice
from apps.web import forms_cyber_updates as forms
from apps.web import views_wo_release as v
from apps.web.forms_wo_complete import CompleteForm
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import WoStatus

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
MR, VL = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST
INSTALLED, PATCHES, ALREADY = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY
# The round 4 words, as written in the fix (fixes38d.md I2)
RELEASE_WORDS = "For Release installed or Already on the update. Patches applied keep the version as recorded."
INSTALLED_HINT = "The release installed, chosen below; the device's record moves to it from that day."
ALREADY_HINT = "It already ran the update when checked; nothing installed."
CONFIRM_LABEL = "Release it ran"
CONFIRM_HELP = "On the day the work order was completed"
# Never in a hint or a help line, as a constraint: the record's words, or the options' marks (information on the options only)
POINTS_AT_THE_RECORD = ("record says", "the recorded one", "recorded before", "· recorded now", "(the device's record now)")


# --- helpers -----------------------------------------------------------------------------------------------------------------------

def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text` as whole words, in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def body_of(r) -> str:
    body = r.content.decode()
    assert not never(body), never(body)
    return body


def section(body: str) -> str:
    """The completion modal's "Software on the device" ("" when there is none)."""
    m = re.search(r'<div class="full fld wc-sw" id="wc-sw".*?<p class="hint wc-sw-help"[^>]*>.*?</p>\s*</div>', body, re.S)
    return m.group(0) if m else ""


def options(body: str, name: str) -> list:
    """(value, label, selected) of the select `name` as rendered."""
    m = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', body, re.S)
    assert m, f"no select {name}"
    return [(value, unescape(label), bool(selected))
            for value, selected, label in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', m.group(1))]


def as_posted(body: str, name: str) -> str:
    """What a browser posts for the select `name` left untouched: the option marked selected, else the first."""
    found = options(body, name)
    chosen = [value for value, _label, selected in found if selected]
    return chosen[0] if chosen else found[0][0]


def hints(body: str) -> dict:
    """{value: hint} of the completion modal's outcome radios."""
    return {value: unescape(hint) for value, hint in
            re.findall(r'<input type="radio" name="software_outcome" value="([^"]*)"[^>]*><span>[^<]*<small>([^<]*)</small>', body)}


def help_lines(body: str) -> list:
    """Every hint and help line a modal shows (the completion section's spans and paragraphs, a device form's help spans)."""
    return [unescape(t) for t in re.findall(r'<(?:span|p) class="hint[^"]*"[^>]*>([^<]*)</(?:span|p)>', body)]


def field_label_and_help(body: str, name: str) -> tuple:
    """(label, help) of a device form's field `name` (web/_asset_field.html: ids "cu-<name>")."""
    label = re.search(rf'<label for="cu-{name}">([^<]*)</label>', body)
    help_ = re.search(rf'<span class="hint" id="cu-{name}_helptext">([^<]*)</span>', body)
    return (unescape(label.group(1)) if label else None, unescape(help_.group(1)) if help_ else None)


def errors(body: str) -> dict:
    """{field name: its error} from a device modal (ids "cu-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cu-(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def toast_of(r) -> str:
    fired = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            fired.update(json.loads(r[h]))
    message = fired.get("toast", {}).get("value", "")
    assert not never(message), never(message)
    return message


def saved(r) -> bool:
    return r.status_code == 200 and r.get("HX-Retarget") == "#drawer"


def facts_now(pump) -> tuple:
    sec = DeviceSecurity.objects.get(asset=pump)
    return sec.release_id, sec.release_since, sec.checked_on


def complete_url(wo) -> str:
    return f"/work-orders/{wo.number}/complete/"


def record_url(asset) -> str:
    return f"/security/devices/{asset.tag}/updates/record/"


def confirm_url(row) -> str:
    return f"/security/updates/{row.pk}/confirm/"


def completion(body: str, outcome: str, *, reference: str = "", basis: str = "", resolution: str = "") -> dict:
    """The completion modal's post with the release select as rendered (the target on a release target)."""
    return {"resolution": resolution, "shown_software_outcome": "1", "software_outcome": outcome,
            "software_release": as_posted(section(body), "software_release"), "software_reference": reference, "software_basis": basis}


def reopen(wo, by):
    """Reopen `wo` (in progress) as stored now (a completion through a view leaves the caller's copy behind)."""
    wo.refresh_from_db()
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=by)


# --- fixtures ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, pump_model, pump, techs, today):
    """The pump model's profile (clinical engineering installs its updates), 5.2 (demo) and 5.6 (demo); CE-10002 on 5.2 (demo) since
    30 days ago, checked then."""
    profile = release(pump_model, updates_by=UpdatesBy.CE)
    r52 = release(pump_model, "5.2 (demo)")
    r56 = release(pump_model, "5.6 (demo)")
    facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    return {"profile": profile, "r52": r52, "r56": r56, "pump": pump, "model": pump_model, "tom": techs["tom"]}


@pytest.fixture
def it_user(tenant):
    """A custom IT role: Device security Edit and Equipment View (records and confirms, never opens work orders)."""
    role = Role.objects.create(name="Itsec", slug="itsec")
    role.set_levels({m: {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username="itsec@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name="Itsec", last_name="User")


def unconfirmed(world, today, target=None, *, reference=""):
    """An update on CE-10002 (to `target`, or the manufacturer's patches with `reference`), completed five days ago with the version
    not confirmed."""
    wo, row = update_wo(world["pump"], target, reference=reference, assigned_to=world["tom"], opened_on=today - 10 * DAY)
    complete_work_order(wo, resolution="Version to be checked", software=UpdateChoice(NOT_CONFIRMED), by=None, today=today - 5 * DAY)
    wo.refresh_from_db()
    return wo, row


# --- 1. the release is for Release installed and Already only: patches send none from every door -----------------------------------

@pytest.mark.parametrize("sent", ["the target", "the recorded one", "one gone or junk"])
def test_the_completion_form_sends_no_release_with_patches(world, sent):
    """CompleteForm (the completion modal's door) drops whatever the select holds for patches applied, as Record update and Confirm
    version do: never a release, never a refusal of the field."""
    r52, r56 = world["r52"], world["r56"]
    ctx = {"outcome_initial": "", "release_choices": [("", v.RELEASE_BLANK), (str(r52.pk), "5.2 (demo)"), (str(r56.pk), "5.6 (demo)")],
           "release_initial": str(r56.pk), "reference_initial": "", "basis_choices": [("", v.BASIS_BLANK), *UpdateBasis.choices]}
    value = {"the target": str(r56.pk), "the recorded one": str(r52.pk), "one gone or junk": "not-an-id"}[sent]
    form = CompleteForm({"resolution": "", "shown_software_outcome": "1", "software_outcome": PATCHES, "software_release": value,
                         "software_reference": "ICAR-2026-09", "software_basis": VL}, offer_software=ctx)
    assert form.is_valid(), form.errors
    assert form.software == UpdateChoice(PATCHES, None, reference="ICAR-2026-09", basis=VL)
    assert form.service_kwargs()["software"].release_id is None
    # installed and already still send the release chosen (a junk one is refused on its field, in the service's words)
    for outcome in (INSTALLED, ALREADY):
        form = CompleteForm({"resolution": "", "shown_software_outcome": "1", "software_outcome": outcome, "software_release": value,
                             "software_reference": "", "software_basis": MR}, offer_software=ctx)
        if sent == "one gone or junk":
            assert not form.is_valid() and form.errors["software_release"] == [updates.CHOOSE_RUNS_NOW]
        else:
            assert form.is_valid(), form.errors
            assert form.software.release_id == value


def test_patches_posted_as_rendered_on_a_release_target_are_recorded(client, make_user, world, today):
    """Skeptics38c web 2 (a): a release target preselects its target; Patches applied posted with the select as rendered sends no release
    and is recorded with the version as the device's record names it, never refused."""
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    client.force_login(make_user("technician"))
    body = body_of(client.get(complete_url(wo), **HX))
    assert as_posted(section(body), "software_release") == str(r56.pk)  # the target, chosen to begin with (for installed and already)
    r = client.post(complete_url(wo), completion(body, PATCHES, reference="ICAR-2026-09", basis=VL), **HX)
    assert saved(r), section(r.content.decode())[:1500]
    assert toast_of(r) == (f"{wo.number} completed; Patches on the manufacturer's validated patch list applied {rules.day_words(today)}, "
                           "version unchanged (5.2 (demo)); ref ICAR-2026-09")
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id, row.reference) == (PATCHES, r52.pk, "ICAR-2026-09")
    assert facts_now(pump) == (r52.pk, today - 30 * DAY, today)


def test_record_update_and_confirm_version_send_no_release_with_patches(client, world, it_user, today):
    """The two device doors: patches with a release left chosen (Record update) or as rendered (Confirm version on a release target, the
    target preselected) are recorded with the version as the record names it."""
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    wo, row = unconfirmed(world, today, r56)
    client.force_login(it_user)
    body = body_of(client.get(confirm_url(row), **MODAL))
    assert as_posted(body, "software_release") == str(r56.pk)
    r = client.post(confirm_url(row), {"software_outcome": PATCHES, "software_release": as_posted(body, "software_release"),
                                       "software_reference": "ICAR-2026-09", "software_basis": MR}, **MODAL)
    assert saved(r), r.content.decode()[:800]
    done = rules.day_words(wo.completed_on)
    assert toast_of(r) == f"{wo.number} confirmed: Patches applied {done}, version unchanged (5.2 (demo)); ref ICAR-2026-09"
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id) == (PATCHES, r52.pk)
    r = client.post(record_url(pump), {"software_outcome": PATCHES, "software_release": str(r56.pk), "software_reference": "ICAR-2026-10",
                                       "software_basis": VL, "installed_by": "it", "applied_on": today.isoformat()}, **MODAL)
    assert saved(r), r.content.decode()[:800]
    assert toast_of(r) == (f"Update recorded for CE-10002: Patches on the manufacturer's validated patch list applied "
                           f"{rules.day_words(today)}, version unchanged (5.2 (demo)); ref ICAR-2026-10; installed by information technology")
    hand = SoftwareUpdate.objects.get(work_order__isnull=True)
    assert (hand.outcome, hand.release_after_id) == (PATCHES, r52.pk)
    assert facts_now(pump)[0] == r52.pk


# --- the release's help, the hints, and Confirm version's label ------------------------------------------------------------------

def test_the_release_help_says_it_is_for_installed_and_already_on_every_door(client, make_user, world, it_user, today):
    pump, r56 = world["pump"], world["r56"]
    assert forms.RELEASE_HELP == v.RELEASE_HINT == RELEASE_WORDS
    assert forms.RECORD_RELEASE_HELP == ("The release installed on the day below, for Release installed. Patches applied keep the version "
                                         "as recorded.")  # it offers no Already
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    client.force_login(make_user("technician"))
    sec = section(body_of(client.get(complete_url(wo), **HX)))
    shown = re.search(r'<span class="hint" id="wc-software_release-hint">([^<]*)</span>', sec)
    assert shown and unescape(shown.group(1)) == RELEASE_WORDS
    assert 'aria-describedby="wc-software_release-hint"' in sec
    client.force_login(it_user)
    record = body_of(client.get(record_url(pump), **MODAL))
    assert field_label_and_help(record, "software_release") == ("Release installed", forms.RECORD_RELEASE_HELP)  # the lead's round 4 follow-up
    _done, row = unconfirmed(world, today, reference="ICAR-2026-09")
    confirm = body_of(client.get(confirm_url(row), **MODAL))
    assert field_label_and_help(confirm, "software_release") == (CONFIRM_LABEL, f"{CONFIRM_HELP}. {RELEASE_WORDS}")


def test_confirm_versions_release_is_the_one_it_ran_on_the_completed_day(client, world, it_user, today):
    """Skeptics38c web 9: a patches update completed five days ago, its device's record moved to 5.6 (demo) two days ago. Confirm
    version's field says the release it ran on the day the work order was completed, never "runs now"; the record's mark stays as
    information."""
    pump, r56 = world["pump"], world["r56"]
    _wo, row = unconfirmed(world, today, reference="ICAR-2026-09")
    facts(pump, r56, release_since=today - 2 * DAY, checked_on=today - 2 * DAY)
    client.force_login(it_user)
    r = client.get(confirm_url(row), **MODAL)
    body = body_of(r)
    form = r.context["form"]
    assert (form.fields["software_release"].label, form.fields["software_release"].help_text) == (CONFIRM_LABEL, forms.CONFIRM_RELEASE_HELP)
    assert forms.CONFIRM_RELEASE_LABEL == CONFIRM_LABEL and forms.CONFIRM_RELEASE_HELP.startswith(CONFIRM_HELP)
    label, help_ = field_label_and_help(body, "software_release")
    assert (label, help_) == (CONFIRM_LABEL, forms.CONFIRM_RELEASE_HELP) and "runs now" not in help_
    assert "Release it runs now" not in unescape(body)
    assert (str(r56.pk), "5.6 (demo) (the device's record now)", False) in options(body, "software_release")  # information, never chosen


def test_the_hints_say_what_is_recorded_and_never_point_at_the_record(client, make_user, world, it_user, today):
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    assert v.OUTCOME_HINTS[INSTALLED] == INSTALLED_HINT and v.OUTCOME_HINTS[ALREADY] == ALREADY_HINT
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    client.force_login(make_user("director"))
    body = body_of(client.get(complete_url(wo), **HX))
    shown = hints(section(body))
    assert shown[INSTALLED] == INSTALLED_HINT and shown[ALREADY] == ALREADY_HINT
    # the mark on the option stays, as information
    assert (str(r52.pk), "5.2 (demo) · recorded now", False) in options(section(body), "software_release")
    lines = [*shown.values(), *help_lines(section(body))]
    client.force_login(it_user)
    lines += help_lines(body_of(client.get(record_url(pump), **MODAL)))
    _done, row = unconfirmed(world, today, reference="ICAR-2026-09")
    lines += help_lines(body_of(client.get(confirm_url(row), **MODAL)))
    words = [*v.OUTCOME_HINTS.values(), v.RELEASE_HINT, v.KEEP_HINT, v.NOT_CONFIRMED_KEEPS, forms.RELEASE_HELP, forms.RECORD_RELEASE_HELP,
             forms.CONFIRM_RELEASE_HELP, forms.REFERENCE_HELP, forms.BASIS_HELP]
    assert len(lines) > 8
    for text in [*lines, *words]:
        assert not [w for w in POINTS_AT_THE_RECORD if w in text.lower()], text
        assert not never(text), text


# --- the skeptics' web scenarios: a truthful result or a plain refusal (with the services' round 4, I1) --------------------------

def test_a_re_completion_after_a_mistaken_install_records_what_the_record_names(client, make_user, world, today):
    """Skeptics38c web 1, 2 (b), 3 (b), 5, 6: CE-10002 on 5.2 (demo); its update to 5.6 (demo) completed as installed by mistake (the
    record moved to 5.6 (demo)), then reopened. Patches with the select as rendered are recorded as the record names the version, which
    the device's facts say too; Already as rendered (the target) is recorded, never refused with words about the record."""
    pump, r56 = world["pump"], world["r56"]
    director = make_user("director")
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=director, today=today)
    reopen(wo, director)
    client.force_login(director)
    body = body_of(client.get(complete_url(wo), **HX))
    assert v.RELEASE_HINT in unescape(section(body)) and "choose the recorded one" not in unescape(section(body))
    r = client.post(complete_url(wo), completion(body, PATCHES, reference="ICAR-2026-09", basis=MR, resolution="Patched"), **HX)
    assert saved(r), section(r.content.decode())[:1500]
    assert toast_of(r) == f"{wo.number} completed; Patches applied {rules.day_words(today)}, version unchanged (5.6 (demo)); ref ICAR-2026-09"
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id) == (PATCHES, r56.pk) == (PATCHES, facts_now(pump)[0])  # the row and the facts agree
    reopen(wo, director)
    body = body_of(client.get(complete_url(wo), **HX))
    r = client.post(complete_url(wo), completion(body, ALREADY, resolution="Checked"), **HX)
    assert saved(r), section(r.content.decode())[:1500]
    assert toast_of(r) == f"{wo.number} completed; Checked {rules.day_words(today)}: already on 5.6 (demo); nothing installed"
    assert "record says" not in r.content.decode()


def test_confirm_version_after_entered_in_error_takes_the_release_named(client, make_user, world, it_user, today):
    """Skeptics38c web 3 (a), 5 (Confirm): the update to 5.6 (demo) completed five days ago as installed 5.6 (demo), its outcome then
    entered in error (the facts stay on 5.6 (demo)). Confirm version's Already with the select as rendered (the target, marked as the
    record) is recorded; no refusal says what the record says."""
    pump, r56 = world["pump"], world["r56"]
    director = make_user("director")
    wo, row = update_wo(pump, r56, assigned_to=world["tom"], opened_on=today - 10 * DAY)
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=director, today=today - 5 * DAY)
    updates.remove(row, by=director, today=today)
    assert facts_now(pump)[0] == r56.pk
    client.force_login(it_user)
    body = body_of(client.get(confirm_url(row), **MODAL))
    assert options(body, "software_release")[-1] == (str(r56.pk), "5.6 (demo) (the device's record now)", True)
    r = client.post(confirm_url(row), {"software_outcome": ALREADY, "software_release": as_posted(body, "software_release"),
                                       "software_reference": "", "software_basis": MR}, **MODAL)
    assert saved(r), errors(r.content.decode())
    assert toast_of(r) == f"{wo.number} confirmed: Checked {rules.day_words(today - 5 * DAY)}: already on 5.6 (demo); nothing installed"
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id, row.basis) == (ALREADY, r56.pk, "")
    assert facts_now(pump)[0] == r56.pk


def test_record_update_after_a_hand_row_entered_in_error_records_patches_as_the_record_names(client, make_user, world, it_user, today):
    """Skeptics38c web 1 (3), 5 (Record update): IT recorded installed 5.6 (demo) two days ago by hand, a director marked it entered in
    error (the facts stay on 5.6 (demo)), and IT records patches that day with the release select untouched: recorded with the version
    the record names, which the facts say too."""
    pump, r56 = world["pump"], world["r56"]
    day = today - 2 * DAY
    hand = updates.record(pump, UpdateChoice(INSTALLED, str(r56.pk), basis=MR), installed_by="it", applied_on=day, by=it_user, today=today)
    updates.remove(hand, by=make_user("director"), today=today)
    client.force_login(it_user)
    body = body_of(client.get(record_url(pump), **MODAL))
    r = client.post(record_url(pump), {"software_outcome": PATCHES, "software_release": as_posted(body, "software_release"),
                                       "software_reference": "ICAR-2026-09", "software_basis": MR, "installed_by": "it",
                                       "applied_on": day.isoformat()}, **MODAL)
    assert saved(r), errors(r.content.decode())
    assert toast_of(r) == (f"Update recorded for CE-10002: Patches applied {rules.day_words(day)}, version unchanged (5.6 (demo)); "
                           "ref ICAR-2026-09; installed by information technology")
    assert facts_now(pump)[0] == r56.pk


def test_the_installed_hint_holds_when_the_record_already_names_the_release(client, make_user, world, today):
    """Skeptics38c web 4, 7: Record facts put CE-10002 on 5.6 (demo) since two days ago; its update to 5.6 (demo) completed today as
    Release installed: the record moves to it from that day, as the hint says."""
    pump, r56 = world["pump"], world["r56"]
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    facts(pump, r56, release_since=today - 2 * DAY, checked_on=today - 2 * DAY)
    client.force_login(make_user("director"))
    body = body_of(client.get(complete_url(wo), **HX))
    assert hints(section(body))[INSTALLED] == INSTALLED_HINT
    r = client.post(complete_url(wo), completion(body, INSTALLED, basis=MR), **HX)
    assert saved(r), section(r.content.decode())[:1500]
    assert toast_of(r) == f"{wo.number} completed; 5.6 (demo) installed {rules.day_words(today)}"
    assert facts_now(pump) == (r56.pk, today, today)
