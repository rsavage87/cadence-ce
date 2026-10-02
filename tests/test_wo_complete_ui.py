"""Completing a work order from its drawer (slice 15): who may open and post the modal, what it shows for a PM and for a repair,
errors kept with what was typed, "Mark the rest pass", the drawer, toasts, and events after a save, the drawer's PM result and
the links between a failed PM and its repair, the printed results, and the device drawer's PM history."""
import json
from datetime import date

import pytest

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.pm.models import PmProcedure
from apps.pm.procedures import update_procedure
from apps.tenants.context import tenant_context
from apps.web import asset_tabs
from apps.workorders import completion
from apps.workorders.completion import checklist_signature, complete_work_order
from apps.workorders.models import PmResult, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
CHECKLIST = ["Inspect & clean", {"text": "Leakage current", "measure": "µA, limit 100"}, {"text": "Battery run time", "measure": True}, "Alarm check"]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def procedure(ctx, vent_model):
    proc = PmProcedure.objects.create(code="HM-G5-PM6", name="Hamilton G5 6-month PM", checklist=CHECKLIST, revision="C")
    vent_model.pm_procedure = proc
    vent_model.save()
    return proc


def started(asset, *, type=WoType.PM, tech=None, vendor="", problem="Scheduled preventive maintenance"):
    wo = create_work_order(asset=asset, type=type, priority="high", problem=problem, source=Source.PM_PLANNER if type == WoType.PM else Source.MANUAL)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


@pytest.fixture
def pm(procedure, vent, techs):
    return started(vent, tech=techs["dana"])


@pytest.fixture
def repair(ctx, vent, techs):
    return started(vent, type=WoType.REPAIR, tech=techs["dana"], problem="Low tidal volume alarm")


def url(wo) -> str:
    return f"/work-orders/{wo.number}/complete/"


def post_data(steps=("pass", "pass", "pass", "pass"), readings=("", "42 µA", "95 min", ""), **extra) -> dict:
    data = {"signature": checklist_signature(completion.checklist_of(PmProcedure.objects.get(code="HM-G5-PM6")))}
    for n, (step, reading) in enumerate(zip(steps, readings), 1):
        if step:
            data[f"step_{n}"] = step
        data[f"reading_{n}"] = reading
    return {**data, **extra}


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


# --- who may --------------------------------------------------------------------------------------------------------------------


def test_signed_out_is_sent_to_sign_in(client, pm):
    r = client.get(url(pm))
    assert r.status_code == 302 and r["Location"].startswith("/login/")


@pytest.mark.parametrize("role", ["director", "manager", "technician", "vendor"])
def test_roles_with_work_orders_edit_open_and_post_it(client, signed_in, repair, role):
    signed_in(role)
    assert client.get(url(repair), **HX).status_code == 200
    r = client.post(url(repair), {"resolution": "Replaced the flow sensor"}, **HX)
    repair.refresh_from_db()
    assert r.status_code == 200 and repair.status == WoStatus.COMPLETED and r["HX-Retarget"] == "#drawer"


@pytest.mark.parametrize("role", ["analyst", "requester"])
def test_view_and_request_roles_get_a_403(client, signed_in, repair, pm, role):
    signed_in(role)
    for wo in (repair, pm):
        assert client.get(url(wo), **HX).status_code == 403
        assert client.post(url(wo), {"resolution": "Fixed", **post_data()}, **HX).status_code == 403
    repair.refresh_from_db()
    pm.refresh_from_db()
    assert repair.status == WoStatus.IN_PROGRESS and pm.status == WoStatus.IN_PROGRESS


def test_another_facilitys_work_order_is_a_404(client, signed_in, other_tenant, ctx):  # no WO-26-0001 here, theirs is
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = create_work_order(asset=Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept), type="repair", priority="normal",
                                   problem="Theirs", vendor_service=True, vendor_name="GE")
        change_status(theirs, WoStatus.IN_PROGRESS)
    signed_in("director")
    assert client.get(url(theirs), **HX).status_code == 404
    assert client.post(url(theirs), {"resolution": "Fixed"}, **HX).status_code == 404
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.status == WoStatus.IN_PROGRESS


def test_the_drawer_opens_the_modal_from_mark_completed(client, signed_in, repair):
    signed_in("technician")
    body = client.get(f"/work-orders/{repair.number}/", **HX).content.decode()
    assert f'hx-get="/work-orders/{repair.number}/complete/" hx-target="#modal-card">Mark completed</button>' in body


# --- the modal --------------------------------------------------------------------------------------------------------------


def test_a_pm_shows_its_checklist_result_choices_and_failure_options(client, signed_in, pm):
    signed_in("technician")
    r = client.get(url(pm), **HX)
    body = r.content.decode()
    assert f"<h2>Complete {pm.number}</h2>" in body and "<b>HM-G5-PM6</b> · Hamilton G5 6-month PM" in body
    assert [row["text"] for row in r.context["rows"]] == ["Inspect & clean", "Leakage current", "Battery run time", "Alarm check"]
    for n in range(1, 5):
        for value, label in (("pass", "Pass"), ("fail", "Fail"), ("na", "N/A")):
            assert f'<input type="radio" name="step_{n}" value="{value}" aria-label="Step {n}: {label}">' in body
    assert body.count('<input class="rd" type="text" name="reading_') == 2  # the two measured steps, under their text
    assert 'name="reading_2" value="" maxlength="60" autocomplete="off" aria-label="Step 2: reading" placeholder="Reading"' in body
    assert 'aria-label="Step 3: reading" placeholder="Reading">' in body and '<small class="muted">Record µA, limit 100</small>' in body
    assert 'name="pm_result" value="pass"' in body and 'name="pm_result" value="pass_minor_repair"' in body and 'name="pm_result" value="fail"' in body
    assert "No patient information." in body and f'name="signature" value="{r.context["form"]["signature"].value()}"' in body
    assert "A repair work order is opened for the failed steps, assigned to Dana Whitfield." in body
    assert 'name="tag_out" id="wc-tag_out" checked' in body and "Tag CE-10001 out of service until the repair is done" in body
    assert "Mark the rest pass" in body and 'hx-vals=\'{"fill": "pass"}\'' in body


def test_a_repair_shows_only_its_resolution(client, signed_in, repair):
    signed_in("technician")
    body = client.get(url(repair), **HX).content.decode()
    assert 'placeholder="What was found and done. No patient information."' in body and "autofocus" in body
    assert "pm_result" not in body and "step_1" not in body and "If the PM failed" not in body


def test_a_pm_without_a_procedure_asks_for_the_resolution(client, signed_in, vent, techs):
    wo = started(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "No PM procedure is on file for this model. Say in the resolution what was checked" in body and "step_1" not in body
    r = client.post(url(wo), {"pm_result": "pass", "signature": checklist_signature([])}, **HX)
    wo.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and wo.pm_result == PmResult.PASS and wo.resolution == "PM completed, all checks passed"


def test_a_work_order_that_cannot_be_completed_says_why(client, signed_in, ctx, vent, techs):
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm", assigned_to=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert f"{wo.number} has not been started. Start work on it first." in body and "<form" not in body
    r = client.post(url(wo), {"resolution": "Fixed"}, **HX)
    wo.refresh_from_db()
    assert "has not been started" in r.content.decode() and wo.status == WoStatus.OPEN and "HX-Retarget" not in r


def test_the_other_open_repair_and_a_device_not_in_service_change_the_options(client, signed_in, pm, vent, techs):
    other = create_work_order(asset=vent, type="repair", priority="normal", problem="Intermittent alarm", assigned_to=techs["dana"])
    vent.status = AssetStatus.IN_REPAIR
    vent.save()
    signed_in("technician")
    body = client.get(url(pm), **HX).content.decode()
    assert 'name="open_repair" id="wc-open_repair" checked' in body and f"CE-10001 already has open repair {other.number}" in body
    assert "CE-10001 is in repair, so there is nothing to tag out." in body and 'name="tag_out"' not in body


def test_errors_come_back_with_what_was_typed(client, signed_in, pm):
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("pass", "fail", "", "pass"), readings=("", "", "95 min", ""), pm_result="pass",
                                       resolution="Found the cord frayed"), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "toast" not in triggers(r)
    assert "Step 2: record the reading." in body and "Step 3: choose pass, fail, or N/A." in body
    assert '<input type="radio" name="step_2" value="fail" aria-label="Step 2: Fail" checked>' in body
    assert 'name="reading_3" value="95 min"' in body and "Found the cord frayed</textarea>" in body
    assert 'name="pm_result" value="pass" checked' in body
    assert 'value="pass" aria-label="Step 2: Pass" autofocus' not in body and 'aria-label="Step 2: reading" placeholder="Reading" autofocus' in body
    pm.refresh_from_db()
    assert pm.status == WoStatus.IN_PROGRESS and pm.pm_result == ""
    r = client.post(url(pm), post_data(pm_result="fail"), **HX)
    assert "Mark the step that failed, or choose another result." in r.content.decode()


def test_a_revised_checklist_is_caught_on_save(client, signed_in, pm, procedure):
    signed_in("technician")
    data = post_data(pm_result="pass")
    update_procedure(procedure, checklist=["Inspect", "Alarm check"])
    body = client.post(url(pm), data, **HX).content.decode()
    assert "The procedure&#x27;s checklist was revised while this was open. Check the steps again." in body
    assert 'name="step_3"' not in body  # the modal now shows the revised checklist


def test_mark_the_rest_pass_keeps_what_was_chosen_and_typed(client, signed_in, pm):
    signed_in("technician")
    params = {"fill": "pass", "step_2": "fail", "reading_2": "180 µA", "step_4": "na", "pm_result": "fail", "resolution": "Leakage high",
              "signature": "abc"}
    r = client.get(url(pm), params, **HX)
    body = r.content.decode()
    assert [row["value"] for row in r.context["rows"]] == ["pass", "fail", "pass", "na"]
    assert 'name="reading_2" value="180 µA"' in body and "Leakage high</textarea>" in body and 'name="pm_result" value="fail" checked' in body
    assert 'name="tag_out" id="wc-tag_out">' in body  # unchecked: it was not in what was sent back
    assert 'name="signature" value="abc"' in body and "choose pass, fail" not in body and 'class="err"' not in body  # nothing checked yet
    pm.refresh_from_db()
    assert pm.status == WoStatus.IN_PROGRESS


# --- saving -------------------------------------------------------------------------------------------------------------------


def test_a_passed_pm_answers_with_the_drawer_and_its_result(client, signed_in, pm, vent):
    signed_in("technician")
    r = client.post(url(pm), post_data(pm_result="pass"), **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    events = triggers(r)
    assert events["toast"]["value"] == f"{pm.number} completed: PM passed" and "wo-changed" in events and "devices-changed" in events
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    body = r.content.decode()
    assert f"<h2>{pm.number} · Preventive maintenance</h2>" in body and "PM result <span>4 steps as recorded" in body
    assert '<span class="chip ok">Pass</span>' in body and "<td>42 µA <span class=\"muted\">· µA, limit 100</span></td>" in body
    assert "PM completed per HM-G5-PM6, all checks passed" in body and "Status changed to completed: PM passed" in body
    pm.refresh_from_db()
    vent.refresh_from_db()
    assert pm.status == WoStatus.COMPLETED and vent.last_pm_on == TODAY


def test_a_failed_pm_opens_its_repair_and_links_both_ways(client, signed_in, pm, vent):
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("pass", "fail", "pass", "pass"), readings=("", "180 µA", "95 min", ""), pm_result="fail",
                                       tag_out="on"), **HX)
    fu = WorkOrder.objects.get(follow_up_of=pm)
    events = triggers(r)
    assert events["toast"]["value"] == f"{pm.number} completed: PM failed; {fu.number} opened for the repair; CE-10001 tagged out of service"
    assert "devices-changed" in events and "wo-changed" in events
    body = r.content.decode()
    assert '<span class="chip crit">Fail, repair work order opened</span>' in body and '<span class="chip crit">Fail</span>' in body
    assert f'<a class="link" href="/work-orders/{fu.number}/" hx-get="/work-orders/{fu.number}/" hx-target="#drawer">{fu.number}</a>' in body
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and fu.tagged_out and fu.assigned_to.name == "Dana Whitfield"
    back = client.get(f"/work-orders/{fu.number}/", **HX).content.decode()
    link = f'<a class="link" href="/work-orders/{pm.number}/" hx-get="/work-orders/{pm.number}/" hx-target="#drawer">{pm.number}</a>'
    assert f"Opened from failed PM {link}" in back
    assert "2. Leakage current (reading 180 µA)" in back


def test_an_unchecked_tag_out_leaves_the_device_in_service(client, signed_in, pm, vent):
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("fail", "pass", "pass", "pass"), pm_result="fail"), **HX)  # tag_out not sent: unchecked
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and "tagged out" not in triggers(r)["toast"]["value"]


def test_options_not_offered_are_not_taken_from_the_post(client, signed_in, pm, vent):
    vent.status = AssetStatus.ON_LOAN
    vent.save()
    signed_in("technician")
    client.post(url(pm), post_data(steps=("fail", "pass", "pass", "pass"), pm_result="fail", tag_out="on"), **HX)  # no open repair: a new one
    vent.refresh_from_db()
    assert vent.status == AssetStatus.ON_LOAN and WorkOrder.objects.filter(follow_up_of=pm).count() == 1


def test_a_failure_recorded_on_the_open_repair(client, signed_in, pm, vent, techs):
    other = create_work_order(asset=vent, type="repair", priority="normal", problem="Intermittent alarm", assigned_to=techs["dana"])
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("fail", "pass", "pass", "pass"), pm_result="fail"), **HX)  # open_repair unchecked
    assert triggers(r)["toast"]["value"] == f"{pm.number} completed: PM failed; recorded on open repair {other.number}"
    assert not WorkOrder.objects.filter(follow_up_of=pm).exists() and other.notes.count() == 1


def test_a_repair_answers_with_its_resolution(client, signed_in, repair):
    signed_in("technician")
    r = client.post(url(repair), {"resolution": "Replaced the flow sensor"}, **HX)
    events = triggers(r)
    assert events["toast"]["value"] == f"{repair.number} completed" and "devices-changed" not in events  # nothing on the device moved
    body = r.content.decode()
    assert "<h3>Resolution</h3>" in body and "Replaced the flow sensor" in body and "PM result" not in body
    error = client.post(url(started(repair.asset, type=WoType.REPAIR, tech=repair.assigned_to)), {"resolution": "  "}, **HX).content.decode()
    assert "Say what was found and done." in error


def test_a_tagged_out_repair_returns_the_device_and_refreshes_the_devices(client, signed_in, ctx, vent, techs):
    wo = create_work_order(asset=vent, type="repair", priority="critical", problem="Dead", tag_out=True, assigned_to=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    r = client.post(url(wo), {"resolution": "Replaced the power supply"}, **HX)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and "devices-changed" in triggers(r)


def test_a_portal_request_says_its_done_email_carries_no_resolution(client, signed_in, ctx, vent, techs):
    sr = create_service_request(asset=vent, department=vent.department, problem="Alarm", urgency="high", requester_email="kim@rrmc.org")
    assign(sr.work_order, technician=techs["dana"])
    change_status(sr.work_order, WoStatus.IN_PROGRESS)
    signed_in("technician")
    assert "the requester's done email never includes this resolution" in client.get(url(sr.work_order), **HX).content.decode()


# --- the drawer -----------------------------------------------------------------------------------------------------------------


def test_a_reopened_pm_says_what_was_recorded(client, signed_in, pm):
    complete_work_order(pm, pm_result=PmResult.PASS, results=["pass", {"result": "pass", "reading": "1"}, {"result": "na"}, "pass"])
    change_status(pm, WoStatus.IN_PROGRESS)
    signed_in("technician")
    body = client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    assert "Reopened. When last completed this PM was recorded as Pass" in body and "PM result <span>" not in body


def test_an_older_completed_pm_shows_no_results_section(client, signed_in, pm):
    change_status(pm, WoStatus.COMPLETED)  # as the API still does
    signed_in("technician")
    body = client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    assert "PM result" not in body and "Reopened" not in body


# --- the print ----------------------------------------------------------------------------------------------------------------


def test_a_completed_pm_prints_what_was_recorded(client, signed_in, pm, procedure):
    complete_work_order(pm, pm_result=PmResult.PASS_MINOR_REPAIR, resolution="Replaced a cracked hose clamp",
                        results=[{"result": "fail"}, {"result": "pass", "reading": "42 µA"}, {"result": "na"}, {"result": "pass"}])
    update_procedure(procedure, checklist=["A different checklist"], revision="D")
    signed_in("analyst")
    body = client.get(f"/print/work-orders/{pm.number}/").content.decode()
    assert '<span class="chip warn">PM result: Pass with minor repair</span>' in body
    assert "<b>Result: Pass with minor repair</b>" in body and '<span class="box">' not in body.split("<h2>PM checklist</h2>")[1].split("<h2>Labor")[0]
    assert '<tr class="failed"><td class="n">1</td><td>Inspect &amp; clean</td><td class="res">Fail</td><td class="meas"></td></tr>' in body
    assert '<td class="res">Pass</td><td class="meas">42 µA <span class="muted">(µA, limit 100)</span></td>' in body
    assert '<td class="res">N/A</td><td class="meas">—</td>' in body and "A different checklist" not in body
    assert "The procedure on file now: HM-G5-PM6, revision D." in body and "Replaced a cracked hose clamp" in body


def test_an_open_pm_still_prints_the_current_checklist(client, signed_in, pm):
    signed_in("technician")
    body = client.get(f"/print/work-orders/{pm.number}/").content.decode()
    assert body.count('<td class="chk"><span class="box"></span></td>') == 4 and "Result:" not in body and "No step results" not in body


def test_an_older_completed_pm_prints_the_checklist_and_says_nothing_was_recorded(client, signed_in, pm):
    change_status(pm, WoStatus.COMPLETED)
    signed_in("technician")
    body = client.get(f"/print/work-orders/{pm.number}/").content.decode()
    assert "No step results were recorded when this PM was completed." in body and body.count('<span class="box"></span></td>') == 4


def test_a_failed_pm_and_its_repair_print_each_other(client, signed_in, pm):
    fu = complete_work_order(pm, pm_result=PmResult.FAIL, results=["fail", {"result": "pass", "reading": "1"}, "na", "pass"]).follow_up
    signed_in("technician")
    body = client.get(f"/print/work-orders/{pm.number}/").content.decode()
    assert f'Repair opened from this PM: <b class="mono">{fu.number}</b> · Open' in body and '<span class="chip bad">PM result: Fail' in body
    back = client.get(f"/print/work-orders/{fu.number}/").content.decode()
    assert f'<dt>Opened from</dt><dd>Failed PM <span class="mono">{pm.number}</span>, done {TODAY:%b} {TODAY.day}, {TODAY.year}</dd>' in back


def test_a_pm_without_a_checklist_prints_its_result(client, signed_in, vent, techs):
    wo = started(vent, tech=techs["dana"])
    complete_work_order(wo, pm_result=PmResult.PASS)
    signed_in("technician")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "<b>Result: Pass</b>" in body and "No checklist was on file when this PM was done" in body and '<div class="rule">' not in body


# --- the device drawer's PM history ------------------------------------------------------------------------------------------


def test_pm_history_shows_recorded_results_and_falls_back_for_older_ones(client, signed_in, vent, procedure, techs):
    old = started(vent, tech=techs["dana"])
    change_status(old, WoStatus.COMPLETED, as_of=TODAY)  # before results were recorded
    passed = started(vent, tech=techs["dana"])
    complete_work_order(passed, pm_result=PmResult.PASS, results=["pass", {"result": "pass", "reading": "1"}, "na", "pass"])
    minor = started(vent, tech=techs["dana"])
    complete_work_order(minor, pm_result=PmResult.PASS_MINOR_REPAIR, results=["fail", {"result": "pass", "reading": "1"}, "na", "pass"],
                        resolution="Replaced a knob")
    failed = started(vent, tech=techs["dana"])
    fu = complete_work_order(failed, pm_result=PmResult.FAIL, results=["fail", {"result": "pass", "reading": "1"}, "na", "pass"]).follow_up
    reopened = started(vent, tech=techs["dana"])
    complete_work_order(reopened, pm_result=PmResult.PASS, results=["pass", {"result": "pass", "reading": "1"}, "na", "pass"])
    change_status(reopened, WoStatus.IN_PROGRESS)
    rows = {h["wo"].number: h for h in asset_tabs.pm_tab(vent)["pm"]["history"]}
    assert rows[old.number]["result"] == "Completed" and not rows[old.number]["has_resolution"]
    assert rows[passed.number]["result"] == "Pass" and rows[minor.number]["result"] == "Pass with minor repair"
    assert rows[failed.number]["result"] == "Fail, repair work order opened" and rows[failed.number]["repair"] == fu
    # reopened: no result until it is completed again; what it shows is the older fallback (its resolution, else its status)
    assert rows[reopened.number]["result"] == "PM completed per HM-G5-PM6, all checks passed" and rows[reopened.number]["repair"] is None
    signed_in("technician")
    body = client.get(f"/equipment/{vent.tag}/?tab=pm", **HX).content.decode()
    assert ('<td class="down" style="white-space:normal;min-width:140px">Fail, repair work order opened · '
            f'<a class="link" href="/work-orders/{fu.number}/" hx-get="/work-orders/{fu.number}/" hx-target="#drawer" style="white-space:nowrap">'
            f'{fu.number}</a></td>') in body
    assert '<td class="warnc" style="white-space:normal;min-width:140px">Pass with minor repair</td>' in body
    assert '<td style="white-space:normal;min-width:140px">Pass</td>' in body


def test_pm_history_reads_the_repairs_in_one_query_only_when_a_pm_failed(ctx, vent, procedure, techs, django_assert_num_queries):
    wos = [started(vent, tech=techs["dana"]) for _ in range(3)]
    for wo in wos:
        complete_work_order(wo, pm_result=PmResult.PASS, results=["pass", {"result": "pass", "reading": "1"}, "na", "pass"])
    history = asset_tabs.pm_history(vent)
    with django_assert_num_queries(0):
        asset_tabs.history_rows(history)
    failed = started(vent, tech=techs["dana"])
    complete_work_order(failed, pm_result=PmResult.FAIL, results=["fail", {"result": "pass", "reading": "1"}, "na", "pass"])
    history = asset_tabs.pm_history(vent)
    with django_assert_num_queries(1):
        asset_tabs.history_rows(history)
