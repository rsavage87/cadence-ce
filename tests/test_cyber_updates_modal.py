"""
Slice 38 wave 2A, a software update on the work order's doors (apps.web.views_wo_release, forms_wo_complete, views_wo_complete,
views_print), on SQLite and PostgreSQL:

- The completion modal's "Software on the device": offered by cybersecurity.rules.release_offer only (a director and a technician; never
  a role without Device security, an analyst (no completing at all), or a scoped vendor account on its company's update; never on
  another type), after the test equipment picker and before the resolution; the target in rules' words, the outcome radios (none chosen
  while a choice is needed; "Keep as recorded" chosen on a re-completion), the releases with the recorded one marked and the target chosen,
  the reference (the target's patch list on a patches target) with its hint, the basis, the help line.
- The post: the marker and no outcome is refused CHOOSE_OUTCOME with the completion's other problems at once, the choices kept and the
  first radio focused; installed, patches, and already through the real view record the outcome (rules' state done; the basis blanked
  for already) and the toast says it in rules' words; not confirmed now needs a resolution and records nothing; Keep as recorded keeps
  it; a release gone meanwhile or junk is refused in the service's words, never a 500; another type and a user without the section post
  nothing.
- The drawer's "Software update" line and the print's, for Device security View and Equipment View only (never a scoped reader; no
  version, reference, or target anywhere in either for a Work orders reader without Device security), in fixed queries; the print's
  query count stays fixed.
- rules.NEVER_WORDS over the modal, the toasts, the drawer line, and the print line in every state.
"""
import json
import re
from datetime import timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from testequipment_fixtures import in_date

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease, SoftwareUpdate, UpdateBasis, UpdateOutcome
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice
from apps.web import views_wo_release as v
from apps.web.forms_wo_complete import CompleteForm
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType

HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)
MR, VL, NS = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST, UpdateBasis.NOT_STATED
INSTALLED, PATCHES, ALREADY = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY
SAY = "Say what was found and done."  # the completion's own refusal of a missing resolution
VERSIONS = ("5.2 (demo)", "5.6 (demo)", "6.0 (demo)", "ICAR-2026-09")
SEEN: list = []  # every page and toast these tests read, for the never-words scan


# --- helpers ---------------------------------------------------------------------------------------------------------------------

def complete_url(wo) -> str:
    return f"/work-orders/{wo.number}/complete/"


def drawer_url(wo) -> str:
    return f"/work-orders/{wo.number}/"


def print_url(wo) -> str:
    return f"/print/work-orders/{wo.number}/"


def read(client, url: str, **extra) -> str:
    body = client.get(url, **extra).content.decode()
    SEEN.append(body)
    return body


def section(body: str) -> str:
    """The modal's "Software on the device" ("" when there is none)."""
    m = re.search(r'<div class="full fld wc-sw" id="wc-sw".*?<p class="hint wc-sw-help"[^>]*>.*?</p>\s*</div>', body, re.S)
    return m.group(0) if m else ""


def radios(body: str) -> list[tuple]:
    """(value, checked, autofocus) of each outcome radio, in order."""
    return [(value, " checked" in attrs, " autofocus" in attrs)
            for value, attrs in re.findall(r'<input type="radio" name="software_outcome" value="([^"]*)"([^>]*)>', section(body))]


def options(body: str, name: str) -> list[tuple]:
    """(value, label, selected) of the section's select `name`."""
    m = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', section(body), re.S)
    if not m:
        return []
    return [(value, unescape(label), bool(selected))
            for value, selected, label in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', m.group(1))]


def field_value(body: str, name: str) -> str | None:
    m = re.search(rf'<input type="text" name="{name}"[^>]*>', section(body))
    if not m:
        return None
    value = re.search(r' value="([^"]*)"', m.group(0))
    return unescape(value.group(1)) if value else ""


def drawer_line(body: str) -> str:
    m = re.search(r'<div class="sec wo-sw" id="wo-sw">.*?</div>', body, re.S)
    return m.group(0) if m else ""


def toast(r) -> str:
    value = json.loads(r["HX-Trigger"])["toast"]["value"]
    SEEN.append(value)
    return value


def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text`, as whole words in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def visible(html: str) -> str:
    """What a reader sees: the text between tags, and the placeholders, titles, and labels read out."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    said = " ".join(re.findall(r'(?:placeholder|title|aria-label)="([^"]*)"', html))
    return unescape(re.sub(r"<[^>]+>", " ", html) + " " + said)


def post(outcome=None, release_=None, *, reference="", basis="", resolution="", shown=True, **extra) -> dict:
    data = {"resolution": resolution, "software_reference": reference, "software_basis": basis, **extra}
    data["software_release"] = str(release_.pk) if isinstance(release_, SoftwareRelease) else (release_ or "")
    if outcome is not None:
        data["software_outcome"] = outcome
    if shown:
        data["shown_software_outcome"] = "1"
    return data


def saved(r) -> bool:
    return r.status_code == 200 and r.get("HX-Retarget") == "#drawer"


def custom(ctx, slug: str, levels: dict, *, scope: str = "", company: str = "") -> User:
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role,
                                    first_name=slug.title(), last_name="User", company=company)


def fresh(user) -> User:
    """The user with its role's levels read once (as a request's would be after its first check): permission checks then need no query."""
    return User.objects.select_related("role").prefetch_related("role__permissions").get(pk=user.pk)


def state(row) -> str:
    return rules.update_state(SoftwareUpdate.objects.select_related("work_order").get(pk=row.pk))


# --- fixtures --------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, val in fields.items():
            setattr(user, k, val)
        user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def r52(pump_model):
    return release(pump_model, "5.2 (demo)")


@pytest.fixture
def r56(pump_model):
    return release(pump_model, "5.6 (demo)")


@pytest.fixture
def r60(pump_model):
    return release(pump_model, "6.0 (demo)")


@pytest.fixture
def on52(pump, r52, today):
    """The pump runs 5.2 (demo), recorded a month ago."""
    return facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)


@pytest.fixture
def job(pump, r56, techs):
    """An update work order to install 5.6 (demo) on the pump, assigned to Tom (open): (work order, its record)."""
    return update_wo(pump, r56, assigned_to=techs["tom"])


@pytest.fixture
def patches_job(pump, techs):
    """An update work order to apply the manufacturer's patches ICAR-2026-09 on the pump, assigned to Tom."""
    return update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])


@pytest.fixture
def vendor_job(pump, r56):
    """An update to install 5.6 (demo) on the pump sent to BD's field service."""
    return update_wo(pump, r56, vendor_service=True, vendor_name="BD field service")


# --- the section: who is offered it, and where -----------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["director", "technician"])
def test_the_section_is_offered_to_who_may_record(client, signed_in, r52, r56, on52, job, role):
    wo, _row = job
    signed_in(role)
    body = read(client, complete_url(wo), **HX)
    sec = section(body)
    assert sec and '<input type="hidden" name="shown_software_outcome" value="1">' in sec
    assert ">Software on the device<" in sec
    assert "Open: to install 5.6 (demo)" in sec  # the target in rules' words, above the radios
    assert sec.index("Open: to install 5.6 (demo)") < sec.index('name="software_outcome"')
    assert radios(body) == [(INSTALLED, False, False), (PATCHES, False, False), (ALREADY, False, False), (NOT_CONFIRMED, False, False)]
    for label in (*(o.label for o in UpdateOutcome), v.NOT_CONFIRMED_LABEL):
        assert unescape(label) in unescape(sec)
    assert options(body, "software_release") == [("", "Not chosen", False), (str(r52.pk), "5.2 (demo) · recorded now", False),
                                                 (str(r56.pk), "5.6 (demo)", True)]  # the recorded one marked, the target chosen
    assert options(body, "software_basis") == [("", "Choose…", True), *((value, label, False) for value, label in UpdateBasis.choices)]
    assert field_value(body, "software_reference") == ""
    assert v.REFERENCE_HINT in unescape(sec) and v.HELP in unescape(sec)


def test_a_patches_target_offers_its_patch_list(client, signed_in, r52, on52, patches_job):
    wo, _row = patches_job
    signed_in("technician")
    body = read(client, complete_url(wo), **HX)
    assert "Open: to apply the manufacturer's patches (ref ICAR-2026-09)" in unescape(section(body))
    assert field_value(body, "software_reference") == "ICAR-2026-09"  # the target's patch list, to begin with
    assert [o for o in options(body, "software_release") if o[2]] == [("", "Not chosen", True)]  # patches leave the version as recorded


def test_a_device_with_no_release_recorded_says_so(client, signed_in, r56, job):
    wo, _row = job
    signed_in("technician")
    body = read(client, complete_url(wo), **HX)
    assert f"Open: to install 5.6 (demo) · {rules.RELEASE_NOT_RECORDED}" in section(body)
    assert "· recorded now" not in section(body)  # no release marked


def test_the_section_comes_after_the_test_equipment_picker_and_before_the_resolution(client, signed_in, r56, job):
    in_date("TE-01")
    wo, _row = job
    signed_in("technician")
    body = read(client, complete_url(wo), **HX)
    assert body.index('class="full wte"') < body.index('id="wc-sw"') < body.index('name="resolution"')


def test_focus_order_puts_the_section_after_the_picker_and_before_the_late_reason_and_resolution():
    ctx = {"outcome_initial": "", "release_choices": [("", "Not chosen")], "release_initial": "", "reference_initial": "",
           "basis_choices": [("", "Choose…")]}

    def first(errors, **kwargs):
        form = CompleteForm({"resolution": ""}, offer_test_equipment=True, offer_late_reason=True, offer_software=ctx, **kwargs)
        form.is_valid()
        for name in errors:
            form.add_error(name, "x")
        form.focus_first_error()
        return form.first_error

    assert first(["software_outcome", "test_equipment"]) == "test_equipment"
    assert first(["software_basis", "late_reason", "resolution"], late_first=True) == "software_basis"
    assert first(["resolution", "software_reference"]) == "software_reference"
    form = CompleteForm({"resolution": "x", "shown_software_outcome": "1", "software_outcome": INSTALLED})  # not offered: no fields
    assert form.is_valid() and not any(name.startswith(("software_", "shown_software")) for name in form.fields)
    assert "software" not in form.service_kwargs()


def test_a_role_without_device_security_sees_no_section_and_posts_nothing(client, ctx, r52, r56, on52, job, today):
    wo, row = job
    clerk = custom(ctx, "wo-clerk", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    client.force_login(clerk)
    body = read(client, complete_url(wo), **HX)
    assert body.count('id="wc-form"') == 1 and section(body) == "" and "shown_software_outcome" not in body
    assert not any(version in body for version in VERSIONS)
    r = client.post(complete_url(wo), post(INSTALLED, r56, basis=MR, resolution="Updated the pump"), **HX)
    assert saved(r) and toast(r) == f"{wo.number} completed"  # the form took nothing from the post: KEEP
    row.refresh_from_db()
    assert row.outcome == "" and state(row) == rules.NOT_CONFIRMED
    assert DeviceSecurity.objects.get(asset=wo.asset).release == r52


def test_an_analyst_completes_nothing(client, signed_in, r56, job):
    wo, _row = job
    signed_in("analyst")  # Work orders View: the modal is not theirs
    assert client.get(complete_url(wo), **HX).status_code == 403
    assert client.post(complete_url(wo), post(NOT_CONFIRMED, resolution="Done"), **HX).status_code == 403


def test_a_scoped_vendor_sees_no_section_and_records_nothing(client, signed_in, r56, vendor_job, today):
    wo, row = vendor_job
    signed_in("vendor", company="BD")
    body = read(client, complete_url(wo), **HX)
    assert body.count('id="wc-form"') == 1 and section(body) == "" and "5.6 (demo)" not in body
    r = client.post(complete_url(wo), post(INSTALLED, r56, basis=MR, resolution="Updated per our visit"), **HX)
    assert saved(r) and toast(r) == f"{wo.number} completed"
    row.refresh_from_db()
    assert row.outcome == "" and not DeviceSecurity.objects.exists()
    assert rules.update_words(SoftwareUpdate.objects.select_related("work_order").get(pk=row.pk)) == (
        f"Completed by the vendor {rules.day_words(today)}; version not confirmed")


def test_another_type_has_no_section_and_posts_nothing(client, signed_in, pump, r56, techs):
    wo = wo_services.create_work_order(asset=pump, type=WoType.SAFETY, priority=Priority.NORMAL, problem="Electrical safety check",
                                       assigned_to=techs["tom"])
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("director")
    body = read(client, complete_url(wo), **HX)
    assert section(body) == "" and "shown_software_outcome" not in body
    r = client.post(complete_url(wo), post(INSTALLED, r56, basis=MR, resolution="Checked"), **HX)
    assert saved(r) and toast(r) == f"{wo.number} completed"
    assert not SoftwareUpdate.objects.exists() and not DeviceSecurity.objects.exists()


def test_an_imported_update_has_no_section(pump, techs, today, make_user):
    from apps.workorders import legacy

    wo = legacy.import_work_order(asset=pump, legacy_number="sw-9", type=WoType.SOFTWARE_UPDATE, status=WoStatus.COMPLETED,
                                  opened_on=today - 20 * DAY, completed_on=today - 19 * DAY, today=today).work_order
    assert v.offer(wo, make_user("director"), today) is None
    assert v.drawer_line(wo, make_user("technician")) is None  # no record: nothing to say


# --- the post ----------------------------------------------------------------------------------------------------------------------

def test_a_choice_is_needed_and_the_modal_keeps_what_was_typed(client, signed_in, r52, r56, on52, job):
    wo, row = job
    signed_in("technician")
    r = client.post(complete_url(wo), post(None, r52, reference="SN-NOTICE-7", basis=NS), **HX)
    body = r.content.decode()
    SEEN.append(body)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert updates.CHOOSE_OUTCOME in section(body) and SAY in body  # every problem at once
    assert radios(body) == [(INSTALLED, False, True), (PATCHES, False, False), (ALREADY, False, False), (NOT_CONFIRMED, False, False)]
    assert [o[0] for o in options(body, "software_release") if o[2]] == [str(r52.pk)]  # as posted, not the target
    assert [o[0] for o in options(body, "software_basis") if o[2]] == [NS]
    assert field_value(body, "software_reference") == "SN-NOTICE-7"
    r = client.post(complete_url(wo), post(None, shown=False, resolution="Done"), **HX)  # the section not on screen: nothing sent
    assert r.status_code == 200 and updates.CHOOSE_OUTCOME in section(r.content.decode())  # the service still asks for a choice
    row.refresh_from_db()
    wo.refresh_from_db()
    assert row.outcome == "" and wo.status == WoStatus.OPEN


def test_installed_through_the_view_records_it_and_says_so(client, signed_in, r52, r56, on52, job, today):
    wo, row = job
    signed_in("technician")
    r = client.post(complete_url(wo), post(INSTALLED, r56, basis=MR), **HX)
    assert saved(r)
    assert toast(r) == f"{wo.number} completed; 5.6 (demo) installed {rules.day_words(today)} (recorded before: 5.2 (demo))"
    row.refresh_from_db()
    wo.refresh_from_db()
    assert state(row) == rules.DONE and (row.outcome, row.release_before, row.release_after, row.basis) == (INSTALLED, r52, r56, MR)
    assert wo.status == WoStatus.COMPLETED and wo.resolution == updates.DEFAULT_RESOLUTION[INSTALLED]
    assert DeviceSecurity.objects.get(asset=wo.asset).release == r56


def test_patches_through_the_view_record_them_and_say_so(client, signed_in, r52, on52, patches_job, today):
    wo, row = patches_job
    signed_in("technician")
    r = client.post(complete_url(wo), post(PATCHES, reference="ICAR-2026-09", basis=VL), **HX)
    assert saved(r)
    assert toast(r) == (f"{wo.number} completed; Patches on the manufacturer's validated patch list applied {rules.day_words(today)}, "
                        "version unchanged (5.2 (demo)); ref ICAR-2026-09")
    row.refresh_from_db()
    assert state(row) == rules.DONE and (row.outcome, row.release_after, row.reference) == (PATCHES, r52, "ICAR-2026-09")


def test_already_through_the_view_blanks_the_basis_and_says_so(client, signed_in, pump, r56, job, today):
    wo, row = job
    facts(pump, r56, release_since=today - 2 * DAY, checked_on=today - 2 * DAY)  # recorded on 5.6 (demo) since it was opened
    signed_in("technician")
    r = client.post(complete_url(wo), post(ALREADY, r56, basis=MR), **HX)  # the basis left on screen is never sent with already
    assert saved(r)
    assert toast(r) == f"{wo.number} completed; Checked {rules.day_words(today)}: already on 5.6 (demo); nothing installed"
    row.refresh_from_db()
    assert state(row) == rules.DONE and (row.outcome, row.basis) == (ALREADY, "")


def test_not_confirmed_needs_a_resolution_and_records_nothing(client, signed_in, r52, r56, on52, job, today):
    wo, row = job
    signed_in("technician")
    r = client.post(complete_url(wo), post(NOT_CONFIRMED, r56, reference="junk ref", basis="nonsense"), **HX)
    body = r.content.decode()
    SEEN.append(body)
    assert r.status_code == 200 and SAY in body and radios(body)[3] == (NOT_CONFIRMED, True, False)
    assert section(body).count('class="err"') == 0  # the rest is never read for not confirmed now
    r = client.post(complete_url(wo), post(NOT_CONFIRMED, r56, resolution="Update done; version to be checked"), **HX)
    assert saved(r) and toast(r) == f"{wo.number} completed"
    row.refresh_from_db()
    assert row.outcome == "" and state(row) == rules.NOT_CONFIRMED
    assert DeviceSecurity.objects.get(asset=wo.asset).release == r52


def test_a_re_completion_keeps_what_was_recorded_by_default(client, signed_in, make_user, r52, r56, on52, job, today):
    wo, row = job
    tech = signed_in("technician")
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=tech, today=today)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=make_user("director"))
    body = read(client, complete_url(wo), **HX)
    assert radios(body) == [(v.KEEP, True, False), (INSTALLED, False, False), (PATCHES, False, False), (ALREADY, False, False),
                            (NOT_CONFIRMED, False, False)]
    assert v.KEEP_LABEL in section(body) and v.KEEP_HINT.format(label=INSTALLED.label) in section(body)
    r = client.post(complete_url(wo), post(v.KEEP, r56, resolution="Reseated the cable"), **HX)
    assert saved(r) and toast(r) == f"{wo.number} completed"  # nothing recorded now: the record stays
    row.refresh_from_db()
    assert (row.outcome, row.release_after) == (INSTALLED, r56) and state(row) == rules.DONE
    assert list(SoftwareUpdate.history.filter(id=row.pk).values_list("outcome", flat=True).order_by("history_date")) == ["", INSTALLED]


def test_a_release_gone_meanwhile_or_junk_is_refused_in_the_services_words(client, signed_in, r60, job):
    wo, row = job
    signed_in("technician")
    body = read(client, complete_url(wo), **HX)
    assert str(r60.pk) in [o[0] for o in options(body, "software_release")]
    SoftwareRelease.objects.filter(pk=r60.pk).update(removed_on=timezone.localdate())  # entered in error while the modal was open
    for value in (str(r60.pk), "not-an-id"):
        r = client.post(complete_url(wo), post(INSTALLED, value, basis=MR, resolution="Done"), **HX)
        assert r.status_code == 200 and updates.CHOOSE_RUNS_NOW in section(r.content.decode())
    r = client.post(complete_url(wo), post("updated", resolution="Done"), **HX)  # no outcome at all
    assert r.status_code == 200 and updates.OUTCOME_WORDS in section(r.content.decode())
    row.refresh_from_db()
    assert row.outcome == ""
    r = client.post(complete_url(wo), post(NOT_CONFIRMED, str(r60.pk), resolution="Version to be checked"), **HX)
    assert saved(r)  # the release is never read for not confirmed now


@pytest.mark.parametrize("case", ["installed the release it ran", "patches with no reference", "already, not on the target",
                                  "installed on the validated list", "installed with nothing chosen", "patches that moved the version"])
def test_the_services_refusals_come_back_on_their_fields(client, signed_in, r52, r56, on52, job, case):
    wo, row = job
    data, key, words = {
        "installed the release it ran": (post(INSTALLED, r52, basis=MR), "software_release", updates.ALREADY_RAN.format(version="5.2 (demo)")),
        "patches with no reference": (post(PATCHES, basis=VL), "software_reference", updates.REFERENCE_NEEDED),
        "already, not on the target": (post(ALREADY, ""), "software_release",
                                       updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")),
        "installed on the validated list": (post(INSTALLED, r56, basis=VL), "software_basis", updates.VALIDATED_PATCHES_ONLY),
        "installed with nothing chosen": (post(INSTALLED, "", basis=MR), "software_release", updates.CHOOSE_RUNS_NOW),
        "patches that moved the version": (post(PATCHES, r56, reference="ICAR-2026-09", basis=VL), "software_release",
                                           updates.PATCHES_UNCHANGED),
    }[case]
    signed_in("technician")
    r = client.post(complete_url(wo), data, **HX)
    body = r.content.decode()
    SEEN.append(body)
    assert r.status_code == 200 and "HX-Retarget" not in r
    sec = section(body)
    assert unescape(sec).count(words) == 1 and SAY in body  # with the completion's other problems at once (nothing was recorded)
    assert [value for value, checked, _focus in radios(body) if checked] == [data["software_outcome"]]  # the choice kept
    assert re.search(rf'<(select|input type="text") name="{key}"[^>]*autofocus', sec)  # the first error has the focus
    assert body.count("autofocus") == 1
    row.refresh_from_db()
    assert row.outcome == ""


# --- the toast's words, for who may see them ------------------------------------------------------------------------------------

def test_the_toast_names_what_was_recorded_only_for_who_may_see_it(ctx, r56, job, make_user):
    wo, row = job
    wo.status = WoStatus.COMPLETED  # as the completion leaves it
    row.outcome, row.release_after, row.applied_on = INSTALLED, r56, timezone.localdate()
    done = SimpleNamespace(software=row)
    assert v.toast_words(wo, done, make_user("technician")).startswith("5.6 (demo) installed")
    assert v.toast_words(wo, done, make_user("analyst")) == ""
    assert v.toast_words(wo, SimpleNamespace(software=None), make_user("director")) == ""


# --- the drawer's line and the print's -------------------------------------------------------------------------------------------

def test_the_drawer_and_print_line_follow_the_record(client, signed_in, pump, r52, r56, on52, job, today):
    wo, row = job
    signed_in("director")
    line = drawer_line(read(client, drawer_url(wo), **HX))
    assert "<b>Software update:</b> Open: to install 5.6 (demo)" in line
    assert f'href="/equipment/{pump.tag}/?tab=security"' in line and v.SECURITY_TAB in unescape(line)
    assert "Confirm version" not in line and "Entered in error" not in line  # the Security tab's, not here
    assert "<b>Software update:</b> Open: to install 5.6 (demo)" in read(client, print_url(wo))
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=None, today=today)
    words = f"5.6 (demo) installed {rules.day_words(today)} (recorded before: 5.2 (demo))"
    assert f"<b>Software update:</b> {words}" in drawer_line(read(client, drawer_url(wo), **HX))
    assert f"<b>Software update:</b> {words}" in read(client, print_url(wo))


def test_the_drawer_line_reads_not_confirmed_and_cancelled(client, signed_in, pump, r56, job, vendor_job, today):
    wo, _row = job
    vendor_wo, _vrow = vendor_job
    complete_work_order(vendor_wo, resolution="Updated per our visit", checks_reference="VR-2026-77", by=None, today=today)
    wo_services.change_status(wo, WoStatus.CANCELLED)
    signed_in("technician")
    assert f"Completed by the vendor {rules.day_words(today)}; version not confirmed" in drawer_line(
        read(client, drawer_url(vendor_wo), **HX))
    assert "<b>Software update:</b> Cancelled" in drawer_line(read(client, drawer_url(wo), **HX))


def test_a_reader_without_device_security_sees_no_line_and_no_version(client, signed_in, ctx, pump, r52, r56, on52, job,
                                                                     patches_job, vendor_job, today):
    wo, _row = job
    patch_wo, _prow = patches_job
    wo_services.change_status(patch_wo, WoStatus.CANCELLED)  # one open update a device at a time: the patches job read cancelled
    vendor_wo, _vrow = vendor_job
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=None, today=today)
    readers = {
        "analyst": signed_in("analyst"),  # Work orders View, Device security None
        "no equipment": custom(ctx, "cyber-no-equipment", {Module.WORKORDERS: Level.VIEW, Module.CYBERSECURITY: Level.VIEW}),
        "scoped with device security": custom(ctx, "bd-it", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW,
                                                             Module.CYBERSECURITY: Level.VIEW}, scope=DataScope.COMPANY, company="BD"),
    }
    for who, user in readers.items():
        client.force_login(user)
        shown = [vendor_wo] if who.startswith("scoped") else [wo, patch_wo, vendor_wo]
        for each in shown:
            for body in (read(client, drawer_url(each), **HX), read(client, print_url(each))):
                assert drawer_line(body) == "" and "Software update:</b>" not in body, (who, each.number)
                assert not any(version in body for version in VERSIONS), (who, each.number)
                assert "Security tab" not in body and "?tab=security" not in body
    client.force_login(custom(ctx, "it-security", {Module.WORKORDERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW,
                                                   Module.CYBERSECURITY: Level.VIEW}))
    assert "5.6 (demo) installed" in drawer_line(read(client, drawer_url(wo), **HX))  # Device security View and Equipment View
    assert "(ref ICAR-2026-09)" not in drawer_line(read(client, drawer_url(patch_wo), **HX))  # cancelled: the target is not said


def test_the_line_reads_one_query_and_none_for_another_type(ctx, pump, r56, job, make_user):
    wo, _row = job
    director = fresh(make_user("director"))
    analyst = fresh(make_user("analyst"))
    other = wo_services.create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    wo, other = (WorkOrder.objects.select_related("asset").get(pk=w.pk) for w in (wo, other))
    with CaptureQueriesContext(connection) as q:
        line = v.drawer_line(wo, director)
    assert len(q) == 1 and line["words"] == "Open: to install 5.6 (demo)" and line["state"] == rules.OPEN
    with CaptureQueriesContext(connection) as q:
        assert v.drawer_line(other, director) is None and v.drawer_line(wo, analyst) is None and v.print_line(other, director) == ""
    assert len(q) == 0


def test_the_drawer_and_print_run_the_same_queries_with_more_updates_on_the_device(client, signed_in, pump, r52, r56, on52, job,
                                                                                    today):
    wo, _row = job
    signed_in("director")
    client.get(drawer_url(wo), **HX)
    with CaptureQueriesContext(connection) as drawer_before:
        client.get(drawer_url(wo), **HX)
    with CaptureQueriesContext(connection) as print_before:
        client.get(print_url(wo))
    for n in range(4):  # installs recorded by hand on the same device, and other update work orders cancelled
        updates.record(pump, UpdateChoice(PATCHES, reference=f"DEMO-PATCH-{n}", basis=NS), installed_by="it",
                       applied_on=today - (10 + n) * DAY, by=None, today=today)
        extra, _ = update_wo(pump, reference=f"ICAR-2026-0{n}")
        wo_services.change_status(extra, WoStatus.CANCELLED)
    with CaptureQueriesContext(connection) as drawer_after:
        client.get(drawer_url(wo), **HX)
    with CaptureQueriesContext(connection) as print_after:
        client.get(print_url(wo))
    assert len(drawer_after) == len(drawer_before) and len(print_after) == len(print_before)


def test_the_offer_reads_the_same_queries_for_few_releases_or_many(client, signed_in, pump_model, r52, r56, on52, job, today):
    wo, _row = job
    tech = fresh(signed_in("technician"))
    wo = WorkOrder.objects.select_related("asset").get(pk=wo.pk)
    with CaptureQueriesContext(connection) as few:
        assert len(v.offer(wo, tech, today)["release_choices"]) == 3
    for n in range(7):
        release(pump_model, f"7.{n} (demo)")
    with CaptureQueriesContext(connection) as many:
        assert len(v.offer(wo, tech, today)["release_choices"]) == 10
    assert len(few) == len(many)
    other = WorkOrder.objects.select_related("asset").get(pk=wo_services.create_work_order(
        asset=wo.asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm").pk)
    with CaptureQueriesContext(connection) as none:
        assert v.offer(other, tech, today) is None
    assert len(none) == 0
    client.get(complete_url(wo), **HX)
    with CaptureQueriesContext(connection) as page_many:
        client.get(complete_url(wo), **HX)
    SoftwareRelease.objects.filter(version__startswith="7.").update(removed_on=today)
    with CaptureQueriesContext(connection) as page_few:
        client.get(complete_url(wo), **HX)
    assert len(page_few) == len(page_many)


# --- the never words ---------------------------------------------------------------------------------------------------------------

def test_no_door_here_says_a_never_word(client, signed_in, make_user, pump, pump_model, dept, r52, r56, on52, techs, today):
    """The modal in every state (a choice needed, each refusal, a patches target, no release recorded, Keep as recorded), each outcome's
    toast, and the drawer's and print's line in every state (open, done each way, not confirmed in-house and by the vendor,
    cancelled), plus this module's words and whatever the tests before it read (SEEN)."""
    from apps.equipment.models import Asset

    seen: list = []
    tom = techs["tom"]

    def get(url, **extra):
        seen.append(client.get(url, **extra).content.decode())

    def send(wo, data):
        r = client.post(complete_url(wo), data, **HX)
        seen.append(r.content.decode())
        if "HX-Trigger" in r:
            seen.append(json.loads(r["HX-Trigger"])["toast"]["value"])
        return r

    def lines(wo):
        get(drawer_url(wo), **HX)
        get(print_url(wo))

    director = make_user("director")
    signed_in("technician")
    wo, _row = update_wo(pump, r56, assigned_to=tom)
    get(complete_url(wo), **HX)  # a choice needed
    for data in (post(None), post(INSTALLED, r52, basis=MR), post(PATCHES, basis=VL), post(ALREADY, ""), post(INSTALLED, r56, basis=VL),
                 post(PATCHES, r56, reference="ICAR 2026", basis=VL), post("updated"), post(NOT_CONFIRMED), post(INSTALLED, "x")):
        assert send(wo, data).status_code == 200
    lines(wo)  # open
    assert saved(send(wo, post(INSTALLED, r56, basis=MR)))
    lines(wo)  # installed
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=director)
    get(complete_url(wo), **HX)  # Keep as recorded
    assert saved(send(wo, post(v.KEEP, resolution="Rechecked the pump")))
    patches, _ = update_wo(pump, reference="ICAR-2026-09", assigned_to=tom)
    get(complete_url(patches), **HX)
    lines(patches)
    assert saved(send(patches, post(PATCHES, reference="ICAR-2026-09", basis=VL)))
    lines(patches)  # patches on the validated list
    already, _ = update_wo(pump, r56, assigned_to=tom)
    assert saved(send(already, post(ALREADY, r56)))
    lines(already)  # already on it
    unconfirmed, _ = update_wo(pump, r52, assigned_to=tom)
    assert saved(send(unconfirmed, post(NOT_CONFIRMED, resolution="Version to be checked")))
    lines(unconfirmed)
    by_vendor, _ = update_wo(pump, r52, vendor_service=True, vendor_name="BD field service")
    complete_work_order(by_vendor, resolution="Updated per our visit", checks_reference="VR-2026-77", by=None, today=today)
    lines(by_vendor)
    cancelled, _ = update_wo(pump, r52, assigned_to=tom)
    wo_services.change_status(cancelled, WoStatus.CANCELLED)
    lines(cancelled)
    bare = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept)
    none_recorded, _ = update_wo(bare, r56, assigned_to=tom)
    get(complete_url(none_recorded), **HX)  # no release recorded
    assert any(rules.RELEASE_NOT_RECORDED in page for page in seen)
    words = [v.LEGEND, v.KEEP_LABEL, v.KEEP_HINT, v.NOT_CONFIRMED_LABEL, *v.OUTCOME_HINTS.values(), v.RELEASE_LABEL, v.RELEASE_HINT,
             v.RELEASE_BLANK, v.RECORDED_MARK, v.REFERENCE_LABEL, v.REFERENCE_HINT, v.BASIS_LABEL, v.BASIS_BLANK, v.HELP, v.ROW_LABEL,
             v.SECURITY_TAB]
    assert len(seen) > 30
    for text in [*(visible(t) for t in [*seen, *SEEN]), *words]:
        assert never(text) == [], (never(text), text[:300])
