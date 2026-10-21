"""
Slice 38 review fixes, round 3, the web forms and hints (H2), on SQLite and PostgreSQL:

- Record update's "Release it runs now" starts on "Choose…" (forms_cyber_updates.RecordUpdateForm: no release to begin with, the
  recorded one only marked "(the device's record now)"). Release installed posted with the select as rendered sends no release and is
  refused (updates.CHOOSE_RUNS_NOW): nothing is written and the device's record, with its since, stays as it was.
- Confirm version starts on the target release of a release target only (ConfirmForm), else on "Choose…": a patches target, or a
  target release entered in error since, preselects nothing, and Release installed posted as rendered is refused the same way.
- The completion modal's "Release installed" hint says what the service records (views_wo_release.OUTCOME_HINTS), never "another
  release": the record already naming the release installed is accepted, as the hint now allows.
- Patches applied send no release from Record update and Confirm version, whatever was chosen (the version is left to the service).
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
from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease, SoftwareUpdate, UpdateBasis, UpdateOutcome, UpdatesBy
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice
from apps.web import forms_cyber_updates as forms
from apps.web import views_wo_release as v
from apps.workorders.completion import complete_work_order

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
MR, VL = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST
INSTALLED, PATCHES = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES
INSTALLED_HINT = "The release installed, chosen below; the device's record moves to it from today"


# --- helpers -----------------------------------------------------------------------------------------------------------------------

def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text` as whole words, in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def body_of(r) -> str:
    body = r.content.decode()
    assert not never(body), never(body)
    return body


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


def errors(body: str) -> dict:
    """{field name: its error} from a modal (ids "cu-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cu-(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def hints(body: str) -> dict:
    """{value: hint} of the completion modal's outcome radios."""
    return {value: unescape(hint) for value, hint in
            re.findall(r'<input type="radio" name="software_outcome" value="([^"]*)"[^>]*><span>[^<]*<small>([^<]*)</small>', body)}


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


def record_url(asset) -> str:
    return f"/security/devices/{asset.tag}/updates/record/"


def confirm_url(row) -> str:
    return f"/security/updates/{row.pk}/confirm/"


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


# --- 1. Record update preselects no release ----------------------------------------------------------------------------------------

def test_record_update_starts_on_choose_and_installed_left_untouched_is_refused(client, world, it_user, today):
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    before = facts_now(pump)
    client.force_login(it_user)
    r = client.get(record_url(pump), **MODAL)
    body = body_of(r)
    assert r.context["form"]["software_release"].value() in (None, "")
    # the recorded release is marked, never chosen: the select starts on "Choose…"
    assert [(value, label) for value, label, _s in options(body, "software_release")] == [
        ("", "Choose…"), (str(r52.pk), "5.2 (demo) (the device's record now)"), (str(r56.pk), "5.6 (demo)")]
    assert [value for value, _l, selected in options(body, "software_release") if selected] in ([], [""])
    assert as_posted(body, "software_release") == ""
    # Release installed, a basis, who, and the day, the release select left as rendered: refused, nothing written
    data = {"software_outcome": INSTALLED, "software_release": as_posted(body, "software_release"), "software_reference": "",
            "software_basis": MR, "installed_by": "it", "applied_on": today.isoformat()}
    r = client.post(record_url(pump), data, **MODAL)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert errors(body_of(r)) == {"software_release": updates.CHOOSE_RUNS_NOW}
    assert not SoftwareUpdate.objects.exists()
    assert facts_now(pump) == before == (r52.pk, today - 30 * DAY, today - 30 * DAY)
    # posted back, the select keeps what was sent ("Choose…"), never the recorded release
    assert as_posted(r.content.decode(), "software_release") == ""


def test_record_update_patches_send_no_release_whatever_was_chosen(world, today):
    form = forms.RecordUpdateForm({"software_outcome": PATCHES, "software_release": str(world["r56"].pk), "software_reference": "ICAR-2026-09",
                                   "software_basis": VL, "installed_by": "it", "applied_on": today.isoformat()},
                                  releases=rules.releases(world["model"].pk), recorded=world["r52"], model_name="BD Alaris 8015 PCU",
                                  today=today)
    assert form.is_valid(), form.errors
    assert form.choice() == UpdateChoice(PATCHES, None, reference="ICAR-2026-09", basis=VL)


# --- 2. Confirm version preselects only the target ---------------------------------------------------------------------------------

def test_confirm_on_a_patches_target_starts_on_choose_and_installed_left_untouched_is_refused(client, world, it_user, today):
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    wo, row = unconfirmed(world, today, reference="ICAR-2026-09")
    before = facts_now(pump)
    client.force_login(it_user)
    r = client.get(confirm_url(row), **MODAL)
    body = body_of(r)
    assert r.context["form"]["software_release"].value() in (None, "")
    assert (str(r52.pk), "5.2 (demo) (the device's record now)", False) in options(body, "software_release")
    assert as_posted(body, "software_release") == ""
    data = {"software_outcome": INSTALLED, "software_release": as_posted(body, "software_release"), "software_reference": "",
            "software_basis": MR}
    r = client.post(confirm_url(row), data, **MODAL)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert errors(body_of(r)) == {"software_release": updates.CHOOSE_RUNS_NOW}
    row.refresh_from_db()
    assert row.outcome == "" and row.release_after_id is None and facts_now(pump) == before
    # chosen deliberately, the install is recorded
    r = client.post(confirm_url(row), {**data, "software_release": str(r56.pk)}, **MODAL)
    assert saved(r), r.content.decode()[:600]
    assert toast_of(r).startswith(f"{wo.number} confirmed: 5.6 (demo) installed {rules.day_words(wo.completed_on)}")


def test_confirm_already_with_a_later_record_left_untouched_names_no_later_version(client, world, it_user, today):
    """The skeptics' later-kept case: a patches update completed five days ago with the version not confirmed while the pump ran 5.2
    (demo); its record set to 5.6 (demo) since two days ago. Already on the update, the select left as rendered, never claims the later
    record's 5.6 (demo) as the version on the completed day."""
    pump, r56 = world["pump"], world["r56"]
    _wo, row = unconfirmed(world, today, reference="ICAR-2026-09")
    facts(pump, r56, release_since=today - 2 * DAY, checked_on=today - 2 * DAY)
    client.force_login(it_user)
    body = body_of(client.get(confirm_url(row), **MODAL))
    assert (str(r56.pk), "5.6 (demo) (the device's record now)", False) in options(body, "software_release")
    assert as_posted(body, "software_release") == ""
    r = client.post(confirm_url(row), {"software_outcome": UpdateOutcome.ALREADY, "software_release": as_posted(body, "software_release"),
                                       "software_reference": "", "software_basis": ""}, **MODAL)
    assert saved(r), r.content.decode()[:600]
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id, row.later_kept) == (UpdateOutcome.ALREADY, None, True)
    assert "5.6 (demo)" not in toast_of(r) and rules.LATER_KEPT_WORDS in toast_of(r)
    assert facts_now(pump) == (r56.pk, today - 2 * DAY, today - 2 * DAY)


def test_confirm_on_a_release_target_starts_on_the_target_only(client, world, it_user, today):
    r52, r56 = world["r52"], world["r56"]
    _wo, row = unconfirmed(world, today, r56)
    client.force_login(it_user)
    r = client.get(confirm_url(row), **MODAL)
    body = body_of(r)
    assert r.context["form"]["software_release"].value() == str(r56.pk)
    assert [value for value, _l, selected in options(body, "software_release") if selected] == [str(r56.pk)]
    assert (str(r52.pk), "5.2 (demo) (the device's record now)", False) in options(body, "software_release")


def test_confirm_on_a_target_entered_in_error_since_starts_on_choose(client, world, it_user, today):
    pump, r52, r56 = world["pump"], world["r52"], world["r56"]
    _wo, row = unconfirmed(world, today, r56)
    SoftwareRelease.objects.filter(pk=r56.pk).update(removed_on=today)  # the target entered in error since: not on file
    before = facts_now(pump)
    client.force_login(it_user)
    r = client.get(confirm_url(row), **MODAL)
    body = body_of(r)
    assert r.context["form"]["software_release"].value() in (None, "")
    assert [(value, label) for value, label, _s in options(body, "software_release")] == [
        ("", "Choose…"), (str(r52.pk), "5.2 (demo) (the device's record now)")]
    assert as_posted(body, "software_release") == ""
    r = client.post(confirm_url(row), {"software_outcome": INSTALLED, "software_release": as_posted(body, "software_release"),
                                       "software_reference": "", "software_basis": MR}, **MODAL)
    assert errors(body_of(r)) == {"software_release": updates.CHOOSE_RUNS_NOW}
    row.refresh_from_db()
    assert row.outcome == "" and facts_now(pump) == before


def test_confirm_patches_send_no_release_whatever_was_chosen(world, today):
    form = forms.ConfirmForm({"software_outcome": PATCHES, "software_release": str(world["r56"].pk), "software_reference": "ICAR-2026-09",
                              "software_basis": VL},
                             releases=rules.releases(world["model"].pk), recorded=world["r52"], target_release=None,
                             model_name="BD Alaris 8015 PCU")
    assert form.is_valid(), form.errors
    assert form.choice() == UpdateChoice(PATCHES, None, reference="ICAR-2026-09", basis=VL)


# --- 3. The completion modal's Release installed hint ------------------------------------------------------------------------------

def test_the_installed_hint_says_what_is_recorded_never_another_release(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    facts(pump, r56, release_since=today, checked_on=today)  # Record facts moved the record to 5.6 (demo) meanwhile
    client.force_login(make_user("director"))
    r = client.get(f"/work-orders/{wo.number}/complete/", **HX)
    shown = hints(body_of(r))
    assert v.OUTCOME_HINTS[INSTALLED] == INSTALLED_HINT
    assert shown[INSTALLED] == INSTALLED_HINT
    assert "another" not in shown[INSTALLED]
    # the release the record names now is offered, and Release installed naming it is accepted, as the hint allows
    assert f'{rules.release_name(r56)} · recorded now' in unescape(r.content.decode())
    r = client.post(f"/work-orders/{wo.number}/complete/", {"resolution": "", "shown_software_outcome": "1", "software_outcome": INSTALLED,
                                                            "software_release": str(r56.pk), "software_reference": "",
                                                            "software_basis": MR}, **HX)
    assert saved(r), r.content.decode()[:600]
    row.refresh_from_db()
    assert (row.outcome, row.release_after_id) == (INSTALLED, r56.pk)
    assert "(recorded before" not in toast_of(r)
