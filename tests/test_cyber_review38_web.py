"""
Slice 38 review fixes, the web doors (F3), on SQLite and PostgreSQL:

- C7, C11: New work order's type refusals are each form's own (forms._TypeField.__deepcopy__): a rental's New work order refuses its
  PM on its own form only; a device of ours, and another facility's device, are offered a PM and get one, never the rental's words.
- C12, C17: on a re-completion with an outcome recorded, "Not confirmed now" says it keeps what was recorded (views_wo_release
  NOT_CONFIRMED_KEEPS), never that Confirm version records it later; posted, the record stays, as it said.
- C13: a completion that records what an update did fires cyber-changed (the Device security screen's lists refetch themselves); one
  recording nothing does not.
- C14: the toasts say "left unassigned" once, worded for how many: "it" for one, "them" for more; round 2: the single-device modal's
  routing line too, the sentence its toast ends with (the batch's modal keeps the plural).
- C18: Entered in error on a device not on site (retired, or returned to its owner) promises neither Confirm version nor Record facts,
  in the modal or the toast: its facts stay as they were (cybersecurity.services._gone's words).
"""
import json
import re
from datetime import timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, release, update_wo
from django.utils import timezone

from apps.accounts.models import create_default_roles
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import DeviceSecurity, InstalledBy, SoftwareRelease, UpdateBasis, UpdateOutcome, UpdatesBy
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.web import forms_cyber_updates as forms
from apps.web import views_cyber_updates as views
from apps.web import views_wo_release as v
from apps.web.forms import NewWorkOrderForm
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import WorkOrder, WoStatus, WoType

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
MR, VL = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST
INSTALLED, PATCHES = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES
RENTAL_PM = "RENT-1 is a rental: its owner maintains it, so it gets no PM work orders here."
SEEN: list = []  # every body and toast read here, for the never-words scan


# --- helpers -----------------------------------------------------------------------------------------------------------------------

def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text` as whole words, in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def body_of(r) -> str:
    body = r.content.decode()
    SEEN.append(body)
    return body


def page(r) -> str:
    """A page outside slice 38's doors (New work order: its priority select says Critical), never scanned for the never words."""
    return r.content.decode()


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    message = triggers(r).get("toast", {}).get("value", "")
    SEEN.append(message)
    return message


def hints(body: str) -> dict:
    """{value: hint} of the completion modal's outcome radios."""
    return {value: unescape(hint) for value, hint in
            re.findall(r'<input type="radio" name="software_outcome" value="([^"]*)"[^>]*><span>[^<]*<small>([^<]*)</small>', body)}


def complete_post(outcome=None, release_=None, *, basis="", resolution="") -> dict:
    data = {"resolution": resolution, "software_reference": "", "software_basis": basis, "shown_software_outcome": "1",
            "software_release": str(release_.pk) if release_ is not None else ""}
    if outcome is not None:
        data["software_outcome"] = outcome
    return data


def saved(r) -> bool:
    return r.status_code == 200 and r.get("HX-Retarget") == "#drawer"


# --- fixtures ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, pump_model, pump, techs, today):
    """The pump model's profile (clinical engineering installs its updates), 5.2 (demo) and 5.6 (demo); CE-10002 on 5.2 (demo)."""
    profile = release(pump_model, updates_by=UpdatesBy.CE)
    r52 = release(pump_model, "5.2 (demo)")
    r56 = release(pump_model, "5.6 (demo)")
    facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    return {"profile": profile, "r52": r52, "r56": r56, "pump": pump, "model": pump_model, "tom": techs["tom"]}


@pytest.fixture
def type_refusals():
    """The declared field's refusals, put back after the test whatever it did (they are the class's, shared by the process)."""
    declared = NewWorkOrderForm.base_fields["type"]
    before = dict(declared.refusals)
    yield before
    declared.refusals.clear()
    declared.refusals.update(before)


# --- C7, C11: New work order's refusals are each form's own -------------------------------------------------------------------------

def test_a_rentals_pm_refusal_stays_on_its_own_form(client, tenant, other_tenant, make_user, dept, pump_model, pump, type_refusals):
    rental = Asset.objects.create(tag="RENT-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    client.force_login(make_user("director"))
    body = page(client.get("/work-orders/new/", {"asset": rental.tag}, **HX))
    assert '<option value="pm"' not in body  # the rental's own form: no PM offered
    assert NewWorkOrderForm.base_fields["type"].refusals == type_refusals  # the declared field is untouched
    # a device of ours, in the same facility: the PM is offered and accepted
    body = page(client.get("/work-orders/new/", {"asset": pump.tag}, **HX))
    assert '<option value="pm"' in body and "RENT-1" not in body
    r = client.post("/work-orders/new/", {"asset": pump.tag, "type": "pm", "priority": "normal", "problem": "Scheduled PM"}, **HX)
    assert RENTAL_PM not in page(r) and "RENT-1" not in r.content.decode()
    assert WorkOrder.objects.filter(asset=pump, type=WoType.PM).count() == 1
    # the rental's own form still refuses its PM, in its own words
    r = client.post("/work-orders/new/", {"asset": rental.tag, "type": "pm", "priority": "normal", "problem": "PM"}, **HX)
    assert RENTAL_PM in page(r) and not WorkOrder.objects.filter(asset=rental).exists()
    # another facility's device: the PM is offered and accepted, and the rental's tag never shows there
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        their_model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = Asset.objects.create(tag="CE-20001", device_model=their_model, department=their_dept)
    client.force_login(make_user("director", other_tenant, username="director@other.example"))
    body = page(client.get("/work-orders/new/", {"asset": theirs.tag}, **HX))
    assert '<option value="pm"' in body and "RENT-1" not in body
    r = client.post("/work-orders/new/", {"asset": theirs.tag, "type": "pm", "priority": "normal", "problem": "Scheduled PM"}, **HX)
    assert "RENT-1" not in page(r)
    with tenant_context(other_tenant):
        assert WorkOrder.objects.filter(asset__tag="CE-20001", type=WoType.PM).count() == 1
    assert NewWorkOrderForm.base_fields["type"].refusals == type_refusals


def test_each_form_has_its_own_refusals(ctx, dept, pump_model, pump, type_refusals):
    rental = Asset.objects.create(tag="RENT-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    first = NewWorkOrderForm(initial={"asset": rental.tag}, can_assign=True)
    assert first.fields["type"].refusals[WoType.PM] == RENTAL_PM
    second = NewWorkOrderForm({"asset": pump.tag, "type": "pm", "priority": "normal", "problem": "Scheduled PM"}, can_assign=True)
    assert WoType.PM not in second.fields["type"].refusals
    assert second.is_valid(), second.errors
    assert first.fields["type"].refusals is not second.fields["type"].refusals is not NewWorkOrderForm.base_fields["type"].refusals
    assert set(second.fields["type"].refusals) == {WoType.SOFTWARE_UPDATE}  # the update's refusal is every form's


# --- C12, C17: Not confirmed now on a re-completion says it keeps the record ---------------------------------------------------------

def test_not_confirmed_now_on_a_re_completion_says_it_keeps_the_record(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    tech = make_user("technician")
    client.force_login(tech)
    first = hints(body_of(client.get(f"/work-orders/{wo.number}/complete/", **HX)))
    assert first[NOT_CONFIRMED] == v.OUTCOME_HINTS[NOT_CONFIRMED]  # nothing recorded yet: Confirm version records it later
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=tech, today=today)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=make_user("director"))
    shown = hints(body_of(client.get(f"/work-orders/{wo.number}/complete/", **HX)))
    assert list(shown) == [v.KEEP, INSTALLED, PATCHES, UpdateOutcome.ALREADY, NOT_CONFIRMED]
    keeps = v.NOT_CONFIRMED_KEEPS.format(label=INSTALLED.label)
    assert shown[NOT_CONFIRMED] == keeps
    assert keeps.startswith("Keeps what was recorded: Release installed;") and "Entered in error" in keeps
    assert "records it later" not in shown[NOT_CONFIRMED] and "Nothing is recorded" not in shown[NOT_CONFIRMED]
    r = client.post(f"/work-orders/{wo.number}/complete/", complete_post(NOT_CONFIRMED, resolution="Could not verify the version"), **HX)
    assert saved(r)
    row.refresh_from_db()
    # as the hint said: what was recorded stays, and Confirm version is not offered on it (Entered in error clears it)
    assert row.outcome == INSTALLED and row.release_after == r56
    assert rules.update_state(type(row).objects.select_related("work_order").get(pk=row.pk)) == rules.DONE
    assert DeviceSecurity.objects.get(asset=pump).release == r56


# --- C13: a recording completion refreshes the Device security screen -----------------------------------------------------------------

def test_a_completion_that_records_a_release_fires_cyber_changed(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    client.force_login(make_user("technician"))
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    r = client.post(f"/work-orders/{wo.number}/complete/", complete_post(INSTALLED, r56, basis=MR), **HX)
    assert saved(r)
    fired = json.loads(r["HX-Trigger"])
    assert {"wo-changed", "cyber-changed", "toast"} <= set(fired)
    assert DeviceSecurity.objects.get(asset=pump).release == r56


def test_a_completion_that_records_nothing_leaves_the_register_alone(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    client.force_login(make_user("technician"))
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    r = client.post(f"/work-orders/{wo.number}/complete/", complete_post(NOT_CONFIRMED, resolution="Version to be checked"), **HX)
    assert saved(r) and "cyber-changed" not in json.loads(r["HX-Trigger"])
    assert DeviceSecurity.objects.get(asset=pump).release == world["r52"]


def test_a_re_completion_keeping_the_record_fires_no_cyber_changed(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    tech = make_user("technician")
    client.force_login(tech)
    wo, _row = update_wo(pump, r56, assigned_to=world["tom"])
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=tech, today=today)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=make_user("director"))
    r = client.post(f"/work-orders/{wo.number}/complete/", complete_post(v.KEEP, resolution="Rechecked the pump"), **HX)
    assert saved(r) and "cyber-changed" not in json.loads(r["HX-Trigger"])


# --- C14: "left unassigned" once, worded for the count -----------------------------------------------------------------------------

def _open_data(world, today, **changes) -> dict:
    data = {"target": "release", "target_release": str(world["r56"].pk), "target_reference": "", "due_on": (today + 10 * DAY).isoformat(),
            "priority": "normal"}
    data.update(changes)
    return data


def _batch_data(dept, today, releases, **changes) -> dict:
    data = {"from_releases": [str(r.pk) for r in releases], "department": str(dept.pk), "due_on": (today + 30 * DAY).isoformat(),
            "priority": "normal"}
    data.update(changes)
    return data


def test_one_update_left_unassigned_says_it_once_in_the_singular(client, make_user, world, today):
    client.force_login(make_user("technician"))
    r = client.post(f"/security/devices/{world['pump'].tag}/updates/open/", _open_data(world, today), **MODAL)
    wo = WorkOrder.objects.get(type=WoType.SOFTWARE_UPDATE)
    message = toast_of(r)
    assert message == (f"{wo.number} opened for CE-10002. Open: to install 5.6 (demo). Left unassigned: assigning needs Work orders "
                       "Approve, so a manager assigns it or a technician takes it.")
    assert message.lower().count("left unassigned") == 1 and " them" not in message


def test_one_update_with_nobody_chosen_says_it_in_the_singular(client, make_user, world, today):
    client.force_login(make_user("director"))
    r = client.post(f"/security/devices/{world['pump'].tag}/updates/open/", _open_data(world, today), **MODAL)
    message = toast_of(r)
    assert message.endswith("Left unassigned: nobody was chosen to assign, so a manager assigns it or a technician takes it.")
    assert message.lower().count("left unassigned") == 1 and " them" not in message


def route_lines(body: str) -> list:
    return [unescape(line) for line in re.findall(r'<p class="full note cu-route">(.*?)</p>', body)]


def test_one_devices_modal_says_left_unassigned_in_the_singular_as_its_toast_does(client, make_user, world, today):
    """Round 2: the single-device Open software update modal's routing line, for a user who may not assign, is worded for the one work
    order it opens (forms_cyber_updates.ASSIGN_NEEDS_ONE), the same sentence its toast ends with once saved; the batch's modal keeps
    the service's plural (updates.ASSIGN_NEEDS)."""
    client.force_login(make_user("technician"))
    url = f"/security/devices/{world['pump'].tag}/updates/open/"
    lines = route_lines(body_of(client.get(url, **MODAL)))
    assert lines == [forms.ASSIGN_NEEDS_ONE]
    assert lines == ["Left unassigned: assigning needs Work orders Approve, so a manager assigns it or a technician takes it."]
    assert " them" not in lines[0] and "takes one" not in lines[0] and updates.ASSIGN_NEEDS not in lines
    message = toast_of(client.post(url, _open_data(world, today), **MODAL))
    assert message.endswith(" " + lines[0])  # what the modal said before saving is what the toast says after
    assert lines[0] == views._left_unassigned(1, updates.ASSIGN_NEEDS, batch=False)
    batch = route_lines(body_of(client.get(f"/security/releases/{world['r56'].pk}/open-updates/", **MODAL)))
    assert batch == [updates.ASSIGN_NEEDS]  # several work orders: "them"


def test_a_directors_single_modal_names_nobody_chosen_nowhere(client, make_user, world, today):
    """A user who may assign is offered Assign to on the single-device modal: no routing line, so never the service's plural
    updates.NOBODY_CHOSEN there (the toast words it for one when nobody is chosen: above)."""
    client.force_login(make_user("director"))
    body = body_of(client.get(f"/security/devices/{world['pump'].tag}/updates/open/", **MODAL))
    assert route_lines(body) == [] and updates.NOBODY_CHOSEN not in unescape(body) and updates.ASSIGN_NEEDS not in unescape(body)


@pytest.fixture
def more_pumps(world, dept, pump_model):
    """P-1 and P-2 on 5.2 (demo) in the ICU, P-5 on 5.2 (demo) in 4 West."""
    west = Department.objects.create(name="4 West")
    devices = {}
    for tag, department in (("P-1", dept), ("P-2", dept), ("P-5", west)):
        devices[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=department)
        facts(devices[tag], world["r52"])
    return {"devices": devices, "west": west}


def test_a_batch_says_left_unassigned_once_for_them_all(client, make_user, world, more_pumps, dept, today):
    client.force_login(make_user("technician"))
    r = client.post(f"/security/releases/{world['r56'].pk}/open-updates/", _batch_data(dept, today, [world["r52"]]), **MODAL)
    message = toast_of(r)
    assert message == ("3 software updates opened to install 5.6 (demo). 3 left unassigned: assigning needs Work orders Approve, so a "
                       "manager assigns them or technicians take them.")
    assert message.lower().count("left unassigned") == 1


def test_a_batch_of_one_says_it_in_the_singular(client, make_user, world, more_pumps, today):
    client.force_login(make_user("technician"))
    data = _batch_data(more_pumps["west"], today, [world["r52"]])
    r = client.post(f"/security/releases/{world['r56'].pk}/open-updates/", data, **MODAL)
    message = toast_of(r)
    assert message == ("1 software update opened to install 5.6 (demo). 1 left unassigned: assigning needs Work orders Approve, so a "
                       "manager assigns it or a technician takes it.")


def test_a_batch_installed_here_by_it_says_left_unassigned_once(client, make_user, world, more_pumps, dept, today):
    SoftwareRelease.objects.filter(pk=world["profile"].pk).update(updates_by=UpdatesBy.IT)
    client.force_login(make_user("director"))
    r = client.post(f"/security/releases/{world['r56'].pk}/open-updates/", _batch_data(dept, today, [world["r52"]]), **MODAL)
    message = toast_of(r)
    assert message == ("3 software updates opened to install 5.6 (demo). 3 left unassigned: updates to this model are installed here by "
                       "information technology.")
    assert message.lower().count("left unassigned") == 1


@pytest.mark.parametrize("reason, count, batch, words", [
    (updates.ASSIGN_NEEDS, 1, False, "Left unassigned: assigning needs Work orders Approve, so a manager assigns it or a technician takes it."),
    (updates.NOBODY_CHOSEN, 2, True, "2 left unassigned: nobody was chosen to assign, so a manager assigns them or technicians take them."),
    (updates.INSTALLED_HERE.format(who="the department that uses it"), 1, False,
     "Left unassigned: updates to this model are installed here by the department that uses it."),
    (updates.INSTALLED_HERE.format(who="the department that uses it"), 4, True,
     "4 left unassigned: updates to this model are installed here by the department that uses it."),
])
def test_each_reason_is_worded_for_the_count(reason, count, batch, words):
    said = views._left_unassigned(count, reason, batch=batch)
    assert said == words and said.lower().count("left unassigned") == 1 and not never(said)


# --- C18: Entered in error on a device not on site ---------------------------------------------------------------------------------

def test_entered_in_error_on_a_retired_devices_update_promises_nothing_it_cannot_do(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    director = make_user("director")
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=director, today=today)
    eq.set_status(pump, AssetStatus.RETIRED, by=director)
    gone = "CE-10002 is retired: its facts stay as they were."
    client.force_login(director)
    body = unescape(body_of(client.get(f"/security/updates/{row.pk}/remove/", **MODAL)))
    assert f"What {wo.number} recorded is cleared: the work order stays as it is, with the version not confirmed. " in body
    assert gone in body and "Confirm version" not in body and "Record facts" not in body
    r = client.post(f"/security/updates/{row.pk}/remove/", {}, **MODAL)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    message = toast_of(r)
    assert message == views.OUTCOME_REMOVED_GONE.format(number=wo.number, gone=gone)
    assert message == f"{wo.number}'s outcome is marked entered in error. {gone}"
    assert "Confirm version" not in message and "Record facts" not in message
    row.refresh_from_db()
    assert row.outcome == ""  # the outcome is cleared all the same
    tab_rows = r.context["cyber"]["updates"]["rows"]
    assert [(u["state"], u["confirm_url"]) for u in tab_rows if u["pk"] == row.pk] == [(rules.NOT_CONFIRMED, "")]  # indeed none


def test_entered_in_error_on_a_returned_rentals_hand_row_says_its_facts_stay(client, make_user, world, dept, pump_model, today):
    rental = Asset.objects.create(tag="RENT-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    facts(rental, world["r52"], release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    hand = updates.record(rental, UpdateChoice(PATCHES, reference="ICAR-2026-09", basis=VL), installed_by=InstalledBy.MANUFACTURER,
                          applied_on=today, by=None)
    Asset.objects.filter(pk=rental.pk).update(status=AssetStatus.RETIRED, returned_on=today)  # returned to its owner
    gone = "RENT-1 was returned to its owner: its facts stay as they were."
    client.force_login(make_user("director"))
    body = unescape(body_of(client.get(f"/security/updates/{hand.pk}/remove/", **MODAL)))
    assert "This update recorded by hand on RENT-1 stays on record, marked entered in error" in body
    assert gone in body and "Record facts" not in body and "Confirm version" not in body
    r = client.post(f"/security/updates/{hand.pk}/remove/", {}, **MODAL)
    message = toast_of(r)
    assert message == f"The update recorded by hand on RENT-1 is marked entered in error. {gone}"
    assert "Record facts" not in message


def test_entered_in_error_on_a_device_on_site_still_offers_both(client, make_user, world, today):
    pump, r56 = world["pump"], world["r56"]
    director = make_user("director")
    wo, row = update_wo(pump, r56, assigned_to=world["tom"])
    complete_work_order(wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR), by=director, today=today)
    client.force_login(director)
    body = unescape(body_of(client.get(f"/security/updates/{row.pk}/remove/", **MODAL)))
    assert "and Confirm version is offered again" in body and "correct it with Record facts first" in body
    r = client.post(f"/security/updates/{row.pk}/remove/", {}, **MODAL)
    assert toast_of(r) == views.OUTCOME_REMOVED.format(number=wo.number)


# --- the never words ---------------------------------------------------------------------------------------------------------------

def test_no_new_word_here_is_a_never_word():
    words = [v.NOT_CONFIRMED_KEEPS, views.HAND_REMOVED_GONE, views.OUTCOME_REMOVED_GONE, views.LEFT_ONE, views.LEFT_UNASSIGNED,
             views.THEN_ONE, views.THEN_MANY, *views.UNASSIGNED_WHY.values(), forms.ASSIGN_NEEDS_ONE]
    for text in [*words, *SEEN]:
        assert not never(text), (never(text), text[:300])
