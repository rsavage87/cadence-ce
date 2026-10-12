"""
Slice 30, checks after a repair, the screens (wave 2A): the completion modal path-first ("After the repair" as radio cards above the
checks, each saying what it does to the device; checked and ready for use chosen to begin with unless the vendor's report is offered;
the vendor's report number only on vendor service for a user who sees the whole facility; the checks with Mark the rest pass, the
number pad on the readings, and no N/A on the visual and function checks), each outcome saved through it with the toast saying what
moved, the refusals shown with what was typed, "If a check fails" (Keep the repair open: completion.keep_open, with the hold-out box
ticked by default), My work's Complete (from=my_work answers without the drawer), the drawer's "Checks after repair" (full page and
partial), and the print (blank boxes while open, the record once done). The last test runs them under the row-level security policies.
"""
import json
from pathlib import Path

import pytest
from django.utils import timezone
from incident_fixtures import held_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, checked_post

from apps.accounts.models import Level, Role, User
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Ownership
from apps.tenants.context import tenant_context
from apps.workorders import completion
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, RepairCheck, Source, WorkOrder, WorkOrderNote, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
VENDOR = "Hamilton Medical field service"
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"


def repair(asset, *, tech=None, vendor="", tag_out=False, start=True, problem="Low tidal volume alarm"):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem=problem, tag_out=tag_out)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def url(wo, what="complete") -> str:
    return f"/work-orders/{wo.number}/{what}/"


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def failed_post(n=3, reading="640 µA", **extra) -> dict:
    """The modal's fields with step `n` failed (its reading when it has one), the rest passing."""
    values = ["pass"] * 7
    values[n - 1] = "fail"
    return checked_post(*values, **{f"reading_{n}": reading, **extra})


def section(body: str, start: str, end: str) -> str:
    return body[body.index(start):body.index(end)]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def today(ctx):
    return timezone.localdate()


# --- the modal, path-first ------------------------------------------------------------------------------------------------------


def test_the_modal_asks_how_it_ended_first_then_the_checks(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    signed_in("technician")
    r = client.get(url(wo), **HX)
    body = r.content.decode()
    # how it ended comes first, then the checks, then the resolution
    assert body.index("After the repair") < body.index("Checks after repair") < body.index('name="step_1"') < body.index('name="resolution"')
    assert '<input type="radio" name="repair_check" value="passed" checked>' in body  # chosen to begin with
    assert '<input type="radio" name="repair_check" value="not_returned">' in body and 'value="vendor_report"' not in body
    assert 'name="checks_reference"' not in body and 'class="wc-form wc-is-repair"' in body and 'class="wc-sec wc-checks"' in body
    assert "Every check recorded, none failed; CE-10001 goes back into service" in body
    assert "Beyond repair, to be retired, or going back; CE-10001 stays out of service: retire it" in body
    # the visual and function checks have no N/A; the electrical ones do; readings bring up the number pad
    assert 'name="step_1" value="na"' not in body and 'name="step_5" value="na"' not in body and body.count("Never N/A</span>") == 2
    assert all(f'name="step_{n}" value="na"' in body for n in (2, 3, 4, 6, 7))
    assert [row["n"] for row in r.context["rows"] if row["never_na"]] == [1, 5]
    assert [row["n"] for row in r.context["rows"] if row["numeric"]] == [2, 3, 4] and body.count('inputmode="decimal"') >= 3
    assert "steps 1 and 5 never are" in body and "Mark the rest pass" in body
    # "If a check fails": keep it open, the device held out by default
    assert "If a check fails" in body and f'hx-post="/work-orders/{wo.number}/keep-open/"' in body
    assert '<input type="checkbox" name="hold" id="wc-hold" checked>' not in body  # a tagged-out repair already holds it: no box
    assert f"{wo.number} already holds CE-10001 out of use until it is completed as checked." in body


def test_the_hold_box_is_ticked_by_default_for_a_device_in_use(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert '<input type="checkbox" name="hold" id="wc-hold" checked> Tag CE-10001 out of service until the repair is checked' in body
    assert '<input type="hidden" name="shown_hold" value="1">' in body
    assert "Every check recorded, none failed; CE-10001 stays in service" in body
    assert "Beyond repair, to be retired, or going back; CE-10001 goes out of service: retire it" in body


def test_the_css_shows_each_part_with_its_choice():
    css = CSS.read_text()
    assert '.wc-form:has(input[name="repair_check"][value="passed"]:checked) .wc-checks{display:block}' in css
    assert '.wc-form:has(input[name="repair_check"][value="vendor_report"]:checked) .wc-reference{display:flex}' in css
    assert '.wc-form:has(.wc-steps input[value="fail"]:checked) .wc-keep{display:grid}' in css


@pytest.mark.parametrize("state", ["other_repair", "waiting", "held_by_hand", "rental", "incident", "incident_no_view"])
def test_each_hint_says_what_the_outcome_does_to_this_device(client, signed_in, tenant, vent, techs, state):
    tech = techs["dana"]
    user = signed_in("technician")
    if state == "other_repair":
        holding = repair(vent, tech=tech, tag_out=True)
        wo = repair(vent, tech=tech)
        checked = f"CE-10001 stays out until {holding.number} is done"
        out = "CE-10001 stays out of service: retire it"
    elif state == "waiting":
        Asset.objects.filter(pk=vent.pk).update(awaiting_inspection=True, status=AssetStatus.OUT_OF_SERVICE)
        wo = repair(vent, tech=tech, tag_out=True)
        checked, out = "CE-10001 stays waiting for its incoming inspection", "CE-10001 stays out of service: retire it"
    elif state == "held_by_hand":
        Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.OUT_OF_SERVICE)
        wo = repair(vent, tech=tech)
        checked, out = "CE-10001 stays out of service until it is returned from its drawer", "CE-10001 stays out of service: retire it"
    elif state == "rental":
        Asset.objects.filter(pk=vent.pk).update(ownership=Ownership.RENTAL)
        wo = repair(vent, tech=tech)
        checked, out = "CE-10001 stays in service", "CE-10001 goes out of service: return it to its owner"
    else:
        incident = held_incident(vent)
        wo = incident.work_order
        assign(wo, technician=tech)
        change_status(wo, WoStatus.IN_PROGRESS)
        checked = out = f"CE-10001 stays held until incident {incident.number} is released"
        if state == "incident_no_view":  # completes work orders, never sees incidents: no number
            role = Role.objects.create(name="Bench", slug="bench")
            role.set_levels({"workorders": Level.EDIT, "equipment": Level.VIEW})
            user = User.objects.create_user(username="bench@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
            client.force_login(user)
            checked = out = "CE-10001 stays held by Clinical Engineering"
    assert user.has_level("incidents", Level.VIEW) == (state != "incident_no_view")
    body = client.get(url(wo), **HX).content.decode()
    assert f"Every check recorded, none failed; {checked}</small>" in body
    assert f"Beyond repair, to be retired, or going back; {out}</small>" in body
    if state.startswith("incident"):
        assert (incident.number in body) == (state == "incident")


def test_the_vendors_report_is_offered_on_vendor_service_to_ce_only_and_nothing_is_chosen(client, signed_in, vent):
    wo = repair(vent, vendor=VENDOR, tag_out=True)
    signed_in("manager")
    r = client.get(url(wo), **HX)
    body = r.content.decode()
    assert 'class="radio wc-results"' in body and 'value="vendor_report"' in body and 'name="checks_reference"' in body
    assert " checked>" not in section(body, "After the repair", "Checks after repair")  # the user chooses
    assert "Their service report&#x27;s number, no steps; CE-10001 goes back into service" in body
    assert "The vendor's service report or field service order number" in body and 'class="full wc-reference"' in body
    # the vendor's own account records the steps: no report choice, and checked and ready for use is chosen (slice 31: their report's
    # number is still taken, as on any vendor job)
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(url(wo), **HX).content.decode()
    assert 'value="vendor_report"' not in body and 'name="checks_reference"' in body and "wc-results-2" in body
    assert '<input type="radio" name="repair_check" value="passed" checked>' in body


# --- each outcome through the modal -----------------------------------------------------------------------------------------------


def test_checked_saves_returns_the_device_and_shows_the_record_in_the_drawer(client, signed_in, vent, techs, today):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    signed_in("technician")
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor"), **HX)
    events = triggers(r)
    assert r["HX-Retarget"] == "#drawer" and events["toast"]["value"] == f"{wo.number} completed: Checked after repair; CE-10001 back in service"
    assert "devices-changed" in events and "wo-changed" in events and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    body = r.content.decode()
    assert f"Checks after repair <span>7 steps as recorded {day(today)}</span>" in body
    assert '<span class="chip ok">Checked: ready for use</span>' in body and f"Checked by Dana Whitfield on {day(today)}" in body
    assert "0.12 Ω" in body and "45 µA" in body
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_checked_by_the_vendor_keeps_no_steps_and_names_the_report(client, signed_in, vent, today):
    wo = repair(vent, vendor=VENDOR, tag_out=True)
    signed_in("manager")
    r = client.post(url(wo), checked_post(repair_check="vendor_report", checks_reference="FSO-118204", resolution="Vendor replaced the blower"),
                    **HX)
    wo.refresh_from_db()
    assert (wo.repair_check, wo.checks_reference, wo.checklist_results) == (RepairCheck.VENDOR_REPORT, "FSO-118204", [])
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Checked by the vendor, report FSO-118204; CE-10001 back in service"
    body = r.content.decode()
    assert "Checks after repair <span>the vendor's service report</span>" in body
    assert f'Checked by {VENDOR} on {day(today)} · service report <b class="mono">FSO-118204</b>' in body


def test_not_returned_takes_a_device_in_use_out_and_the_toast_says_so(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo), checked_post(repair_check="not_returned", resolution="Cracked chassis, beyond economical repair"), **HX)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    events = triggers(r)
    assert events["toast"]["value"] == f"{wo.number} completed: Not returned to use: CE-10001 tagged out of service" and "devices-changed" in events
    body = r.content.decode()
    assert "Checks after repair <span>none: not returned to use</span>" in body and '<span class="chip warn">Not returned to use</span>' in body
    assert "CE-10001 did not go back into use after this repair" in body and "Reopening it and completing it as checked returns it." in body


def test_not_returned_on_a_device_already_out_says_it_stays_out(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    signed_in("technician")
    r = client.post(url(wo), {"repair_check": "not_returned", "resolution": "Beyond repair"}, **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Not returned to use: CE-10001 stays out of service"


def test_a_reopened_repair_says_what_was_recorded_and_checked_returns_the_device(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    complete_work_order(wo, resolution="Sent to the depot", repair_check=RepairCheck.NOT_RETURNED)
    change_status(wo, WoStatus.IN_PROGRESS)  # back from the depot, put right
    signed_in("technician")
    drawer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert ("Reopened. When last completed this repair was recorded as not returned to use; completing it again records its checks after "
            "repair anew.") in drawer
    body = client.get(url(wo), **HX).content.decode()
    assert "Every check recorded, none failed; CE-10001 goes back into service" in body  # as if checked now, not as recorded
    r = client.post(url(wo), checked_post(resolution="Depot replaced the board"), **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Checked after repair; CE-10001 back in service"


# --- refusals, with what was typed --------------------------------------------------------------------------------------------------


def test_no_outcome_is_refused_with_nothing_chosen_and_the_first_choice_focused(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.post(url(wo), {"resolution": "Fixed the latch"}, **HX).content.decode()
    assert "Choose how the repair ended: checked and ready for use or not returned to use." in body
    assert '<input type="radio" name="repair_check" value="passed" autofocus>' in body and "Fixed the latch</textarea>" in body
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS


def test_a_never_na_step_and_a_failed_step_are_refused_at_the_step_with_the_values_kept(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    data = checked_post("na", "pass", "fail", "pass", "pass", "pass", "pass", reading_3="640 µA", resolution="Replaced the line cord")
    r = client.post(url(wo), data, **HX)
    body = r.content.decode()
    assert "Step 1 is never N/A after a repair: mark it pass or fail." in body and completion.FAILED_CHECK in body
    assert '<input type="radio" name="repair_check" value="passed" checked>' in body
    assert 'name="step_3" value="fail" aria-label="Step 3: Fail" checked' in body and 'value="640 µA"' in body
    assert 'name="step_2" value="pass" aria-label="Step 2: Pass" checked' in body and "Replaced the line cord</textarea>" in body
    assert 'name="step_1" value="pass" aria-label="Step 1: Pass" autofocus' in body  # the first error, focused
    assert [row["value"] for row in r.context["rows"]][1:] == ["pass", "fail", "pass", "pass", "pass", "pass"]
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.repair_check == ""


@pytest.mark.parametrize("data, message", [
    ({"repair_check": "passed"}, completion.NO_CHECKS),
    (checked_post(reading_2=""), "Step 2: record the reading."),
])
def test_missing_checks_are_refused_in_words(client, signed_in, vent, techs, data, message):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.post(url(wo), {"resolution": "Fixed", **data}, **HX).content.decode()
    assert message in body and '<input type="radio" name="repair_check" value="passed" checked>' in body


def test_checks_typed_against_an_older_list_show_the_steps_anew(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo), checked_post(signature="0123456789abcdef", resolution="Fixed"), **HX)
    body = r.content.decode()
    assert completion.REPAIR_REVISED in body and all(row["value"] == "" for row in r.context["rows"])
    assert f'name="signature" value="{completion.checklist_signature(completion.checklist_of(completion.procedure_for(wo)))}"' in body


def test_the_vendors_report_number_is_refused_in_words_and_kept(client, signed_in, vent):
    wo = repair(vent, vendor=VENDOR)
    signed_in("manager")
    body = client.post(url(wo), {"repair_check": "vendor_report", "checks_reference": "FSO 12", "resolution": "Vendor fixed it"},
                       **HX).content.decode()
    assert "as it is printed, without spaces" in body and '<input type="radio" name="repair_check" value="vendor_report" checked>' in body
    assert 'value="FSO 12"' in body and 'autofocus aria-invalid="true" aria-describedby="wc-checks_reference_error"' in body
    assert '<span class="err" id="wc-checks_reference_error">' in body
    body = client.post(url(wo), {"repair_check": "vendor_report", "resolution": "Vendor fixed it"}, **HX).content.decode()
    assert "Enter the vendor&#x27;s service report or field service order number." in body


def test_the_vendors_own_account_cannot_record_the_vendors_report(client, signed_in, vent):
    wo = repair(vent, vendor=VENDOR)
    signed_in("vendor", company="Hamilton Medical")
    body = client.post(url(wo), {"repair_check": "vendor_report", "checks_reference": "FSO-12", "resolution": "Fixed"}, **HX).content.decode()
    assert "Record the checks after the repair step by step." in body
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS


def test_mark_the_rest_pass_keeps_the_outcome_the_failed_step_and_the_hold(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.get(url(wo), {"fill": "pass", "repair_check": "passed", "step_3": "fail", "reading_3": "640 µA", "shown_hold": "1"}, **HX)
    body = r.content.decode()
    assert [row["value"] for row in r.context["rows"]] == ["pass", "pass", "fail", "pass", "pass", "pass", "pass"]
    assert '<input type="radio" name="repair_check" value="passed" checked>' in body and 'value="640 µA"' in body
    assert '<input type="checkbox" name="hold" id="wc-hold">' in body  # unticked on screen: kept unticked


# --- if a check fails: keep the repair open -----------------------------------------------------------------------------------------


def test_keep_open_records_the_failed_check_and_tags_the_device_out(client, signed_in, vent, techs, today):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo, "keep-open"), failed_post(shown_hold="1", hold="on"), **HX)
    events = triggers(r)
    assert r["HX-Retarget"] == "#drawer" and "devices-changed" in events and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert events["toast"]["value"] == f"{wo.number} kept open: failed checks recorded; CE-10001 tagged out of service"
    note = WorkOrderNote.objects.get(work_order=wo).text
    assert note == f"Checks after repair failed on {day(today)}: 3. Equipment (touch) leakage (N/A as for protective earth) (reading 640 µA)"
    assert note in r.content.decode()  # the drawer's timeline
    wo.refresh_from_db()
    vent.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.tagged_out and wo.repair_check == "" and vent.status == AssetStatus.OUT_OF_SERVICE
    # put right and checked: back in service
    r = client.post(url(wo), checked_post(resolution="Replaced the line cord"), **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Checked after repair; CE-10001 back in service"


def test_keep_open_with_the_box_unticked_only_records(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo, "keep-open"), failed_post(shown_hold="1"), **HX)
    events = triggers(r)
    assert events["toast"]["value"] == f"{wo.number} kept open: failed checks recorded" and "devices-changed" not in events
    vent.refresh_from_db()
    wo.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and not wo.tagged_out and wo.notes.count() == 1


def test_keep_open_on_a_device_in_repair_holds_it_without_moving_it(client, signed_in, pump, techs):
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.IN_REPAIR)
    wo = repair(pump, tech=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "Hold CE-10002 out of use until the repair is checked (it stays in repair meanwhile)" in body
    r = client.post(url(wo, "keep-open"), failed_post(2, "2.4 Ω", shown_hold="1", hold="on"), **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} kept open: failed checks recorded; CE-10002 held out of use until it is checked"
    assert "devices-changed" not in triggers(r)
    pump.refresh_from_db()
    wo.refresh_from_db()
    assert pump.status == AssetStatus.IN_REPAIR and wo.tagged_out


def test_keep_open_with_no_box_on_screen_holds_by_default(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    signed_in("technician")
    r = client.post(url(wo, "keep-open"), failed_post(), **HX)  # no shown_hold: the box was not offered
    assert triggers(r)["toast"]["value"] == f"{wo.number} kept open: failed checks recorded; CE-10001 held out of use until it is checked"


@pytest.mark.parametrize("data, message", [
    (checked_post(), "No check failed. Complete the repair as checked and ready for use."),
    ({"repair_check": "passed"}, "Record the check that failed, with its reading when it has one."),
    (failed_post(reading=""), "Step 3: record the reading."),
])
def test_keep_open_refusals_come_back_with_the_values_kept(client, signed_in, vent, techs, data, message):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo, "keep-open"), {"resolution": "Still alarming", **data}, **HX)
    body = r.content.decode()
    assert "HX-Retarget" not in r and message in body and "Still alarming</textarea>" in body
    assert [row["value"] for row in r.context["rows"]] == [data.get(f"step_{n}", "") for n in range(1, 8)]  # never wiped
    assert not WorkOrderNote.objects.filter(work_order=wo).exists()


def test_keep_open_doors(client, signed_in, make_user, vent, pump, techs, other_tenant):
    wo = repair(vent, tech=techs["dana"])
    assert client.post(url(wo, "keep-open"), failed_post(), **HX).status_code == 302  # signed out: to sign in
    signed_in("analyst")  # Work orders View
    assert client.post(url(wo, "keep-open"), failed_post(), **HX).status_code == 403
    client.force_login(make_user("requester", username="req@riverside.example"))
    assert client.post(url(wo, "keep-open"), failed_post(), **HX).status_code == 403
    signed_in("technician")
    assert client.get(url(wo, "keep-open"), **HX).status_code == 405
    pm = create_work_order(asset=pump, type=WoType.PM, priority="normal", problem="PM", assigned_to=techs["dana"])
    assert client.post(url(pm, "keep-open"), failed_post(), **HX).status_code == 404
    assert client.post("/work-orders/WO-99-9999/keep-open/", failed_post(), **HX).status_code == 404
    not_started = repair(pump, tech=techs["dana"], start=False)
    body = client.post(url(not_started, "keep-open"), failed_post(), **HX).content.decode()
    assert f"{not_started.number} has not been started. Start work on it first." in body and "wc-form" not in body
    assert not WorkOrderNote.objects.exists()


def test_a_vendors_own_account_keeps_its_repair_open(client, signed_in, vent):
    wo = repair(vent, vendor=VENDOR)
    signed_in("vendor", company="Hamilton Medical")
    assert "If a check fails" in client.get(url(wo), **HX).content.decode()
    r = client.post(url(wo, "keep-open"), failed_post(shown_hold="1", hold="on"), **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} kept open: failed checks recorded; CE-10001 tagged out of service"


def test_keep_open_is_offered_on_a_repair_only(client, signed_in, pump, techs):
    pm = create_work_order(asset=pump, type=WoType.PM, priority="normal", problem="PM", assigned_to=techs["dana"])
    signed_in("technician")
    body = client.get(url(pm), **HX).content.decode()
    assert "If a check fails" not in body and "keep-open" not in body and "After the repair" not in body


# --- My work's Complete -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def mine(signed_in, vent, techs):
    """A repair in progress on My work: the technician signed in is Dana Whitfield."""
    user = signed_in("technician")
    Technician.objects.filter(pk=techs["dana"].pk).update(user=user)
    return repair(vent, tech=techs["dana"])


def test_my_works_complete_opens_the_same_modal(client, mine):
    page = client.get("/my-work/", **HX).content.decode()
    assert f'hx-get="/work-orders/{mine.number}/complete/" hx-vals=\'{{"from": "my_work"}}\'' in page
    body = client.get(url(mine), {"from": "my_work"}, **HX).content.decode()
    assert '<input type="hidden" name="from" value="my_work">' in body and "After the repair" in body and "If a check fails" in body


@pytest.mark.parametrize("data, toast", [
    (checked_post(resolution="Replaced the flow sensor"), "Checked after repair"),
    ({"repair_check": "not_returned", "resolution": "Beyond repair"}, "Not returned to use: CE-10001 tagged out of service"),
])
def test_from_my_work_a_completion_answers_without_the_drawer(client, mine, data, toast):
    r = client.post(url(mine), {**data, "from": "my_work"}, **HX)
    assert r["HX-Reswap"] == "none" and "HX-Retarget" not in r and r.content == b""
    assert triggers(r)["toast"]["value"] == f"{mine.number} completed: {toast}" and "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_from_my_work_keep_open_answers_without_the_drawer(client, mine):
    r = client.post(url(mine, "keep-open"), failed_post(shown_hold="1", hold="on", **{"from": "my_work"}), **HX)
    assert r["HX-Reswap"] == "none" and "HX-Retarget" not in r and "wo-changed" in triggers(r)
    assert triggers(r)["toast"]["value"] == f"{mine.number} kept open: failed checks recorded; CE-10001 tagged out of service"
    body = client.post(url(mine, "keep-open"), {**checked_post(), "from": "my_work"}, **HX).content.decode()
    assert "No check failed." in body and 'name="from" value="my_work"' in body  # kept through the refusal


# --- the drawer -----------------------------------------------------------------------------------------------------------------------


def test_the_drawers_checks_show_as_a_full_page_and_as_the_drawer(client, signed_in, vent, techs, today):
    wo = repair(vent, tech=techs["dana"])
    complete_work_order(wo, resolution="Replaced the flow sensor", **CHECKED)
    signed_in("analyst")  # Work orders View reads them
    for headers in ({}, HX):
        body = client.get(f"/work-orders/{wo.number}/", **headers).content.decode()
        assert f"Checks after repair <span>7 steps as recorded {day(today)}</span>" in body and "Checked by Dana Whitfield" in body
        assert ("<html" in body) == (headers == {})


def test_a_repair_completed_with_no_outcome_says_so(client, signed_in, vent, techs):
    old = repair(vent, tech=techs["dana"])
    change_status(old, WoStatus.COMPLETED)  # completed before slice 30
    imported = repair(vent, tech=techs["dana"])
    change_status(imported, WoStatus.COMPLETED)
    WorkOrder.objects.filter(pk=imported.pk).update(source=Source.IMPORTED)
    signed_in("technician")
    assert "No checks after repair were recorded with this repair." in client.get(f"/work-orders/{old.number}/", **HX).content.decode()
    body = client.get(f"/work-orders/{imported.number}/", **HX).content.decode()
    assert "Imported from the previous system: no checks after repair recorded here." in body
    assert "Checks after repair" not in client.get(f"/work-orders/{repair(vent, tech=techs['dana']).number}/", **HX).content.decode()


# --- the print ------------------------------------------------------------------------------------------------------------------------


def test_an_open_repair_prints_blank_boxes_for_paper(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    vendor = repair(vent, vendor=VENDOR)
    signed_in("technician")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    block = section(body, "<h2>Checks after repair</h2>", "<h2>Labor</h2>")
    assert block.count('<span class="box"></span>') == 7 * 3 - 2 + 2  # pass, fail, N/A per step (none on 1 and 5), and two outcomes
    assert "Protective earth resistance" in block and '<span class="blank"></span> Ω' in block and block.count("leakage µA") == 2
    assert "Checked: ready for use" in block and "Not returned to use" in block and "Checked by the vendor" not in block
    assert '<div class="sign"><div>Checked by</div><div>Date</div></div>' in block and "<h2>Findings and repair</h2>" in body
    block = section(client.get(f"/print/work-orders/{vendor.number}/").content.decode(), "<h2>Checks after repair</h2>", "<h2>Labor</h2>")
    assert block.count('<span class="box"></span>') == 7 * 3 - 2 + 3 and 'Checked by the vendor, service report <span class="blank"></span>' in block


def test_a_done_repair_prints_its_record(client, signed_in, vent, techs, today):
    checked = repair(vent, tech=techs["dana"])
    signed_in("technician")
    client.post(url(checked), checked_post(resolution="Replaced the flow sensor"), **HX)
    body = client.get(f"/print/work-orders/{checked.number}/").content.decode()
    assert "After the repair: Checked: ready for use" in body
    assert f"<b>Checked: ready for use</b> · checked by Dana Whitfield · completed {day(today)}" in body and "0.12 Ω" in body
    assert '<span class="box"></span> Not returned to use' not in body
    vendor = repair(vent, vendor=VENDOR)
    complete_work_order(vendor, resolution="Vendor fixed it", repair_check=RepairCheck.VENDOR_REPORT, checks_reference="FSO-118204")
    body = client.get(f"/print/work-orders/{vendor.number}/").content.decode()
    assert f'· checked by {VENDOR} · service report <span class="mono">FSO-118204</span>' in body
    gone = repair(vent, tech=techs["dana"])
    complete_work_order(gone, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED)
    body = client.get(f"/print/work-orders/{gone.number}/").content.decode()
    assert "CE-10001 did not go back into use after this repair, so no checks were recorded." in body and "checked by" not in body
    old = repair(vent, tech=techs["dana"])
    change_status(old, WoStatus.COMPLETED)
    assert "No checks after repair were recorded with this repair." in client.get(f"/print/work-orders/{old.number}/").content.decode()
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert "Checks after repair" not in client.get(f"/print/work-orders/{cancelled.number}/").content.decode()


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_screens_under_the_policies(client, signed_in, tenant, vent, pump, techs):
    """As the runtime role: the modal, Keep open, the completion, the drawer, and the print read and write in the request's facility."""
    held = repair(vent, tech=techs["dana"], tag_out=True)
    kept = repair(pump, tech=techs["dana"])
    signed_in("technician")
    as_app_role()
    assert "CE-10001 goes back into service" in client.get(url(held), **HX).content.decode()
    r = client.post(url(kept, "keep-open"), failed_post(shown_hold="1", hold="on"), **HX)
    assert triggers(r)["toast"]["value"] == f"{kept.number} kept open: failed checks recorded; CE-10002 tagged out of service"
    r = client.post(url(held), checked_post(resolution="Replaced the flow sensor"), **HX)
    assert triggers(r)["toast"]["value"] == f"{held.number} completed: Checked after repair; CE-10001 back in service"
    assert "Checked by Dana Whitfield" in r.content.decode()
    assert "<b>Checked: ready for use</b>" in client.get(f"/print/work-orders/{held.number}/").content.decode()
    with tenant_context(tenant):
        vent.refresh_from_db()
        pump.refresh_from_db()
        assert vent.status == AssetStatus.IN_SERVICE and pump.status == AssetStatus.OUT_OF_SERVICE
        assert WorkOrderNote.objects.filter(work_order=kept).count() == 1
