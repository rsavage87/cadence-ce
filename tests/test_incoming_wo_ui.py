"""
Slice 26, B2: incoming inspections on the work order screens. Mark completed on an inspection (its checklist, the incoming one or the
model's procedure; its result, required while the device waits and saying what each choice does to the device, optional otherwise; a
waiting device's "If it fails": the re-inspection and a device in use's tag-out; a pass kept on a device that no longer waits), the
toasts and the drawer after a save (from My work too), the drawer's results section and the links between a failed inspection and its
re-inspection, the print, My work's cards, the Work orders page's note for managers, and New work order (the type and problem filled
in from the address; create_work_order's refusals on the form). Scoped vendors complete their own and never see another's numbers.
The services are tests/test_incoming_services.py and tests/test_incoming_completion.py.
"""
import json
import re

import pytest
from django.utils import timezone
from incoming_fixtures import LEAKAGE, failed_device, in_use_device, passed_device, waiting_device

from apps.credentials.models import Technician
from apps.equipment.models import AssetStatus
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.web import views_my_work
from apps.workorders import completion, inspections
from apps.workorders.completion import checklist_of, checklist_signature, complete_work_order, procedure_for
from apps.workorders.models import InspectionResult, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
PASSED, FAILED = InspectionResult.PASSED, InspectionResult.FAILED
CHECKLIST = ["Inspect and clean", {"text": "Leakage current", "measure": "µA, limit 100"}, "Alarm check"]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, *, company="", profile=""):
        user = make_user(role_slug)
        if company:
            user.company = company
            user.save()
        if profile:
            Technician.objects.create(user=user, name=profile)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def waiting(ctx, today, vent_model, techs):
    """NEW-1, a ventilator waiting for its incoming inspection, the inspection assigned to Dana and not started."""
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, technician=techs["dana"])
    return asset, wo


def url(wo, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/work-orders/{wo.number}/complete/" + (f"?{query}" if query else "")


def steps_data(wo, values=None, reading=LEAKAGE) -> dict:
    """The modal's post for the inspection's checklist: each step pass / fail / na (all pass when not given), readings on measured steps."""
    steps = checklist_of(procedure_for(wo))
    values = values or ["pass"] * len(steps)
    data = {"signature": checklist_signature(steps)}
    for n, ((_text, measure), value) in enumerate(zip(steps, values, strict=True), 1):
        data[f"step_{n}"] = value
        data[f"reading_{n}"] = reading if measure is not None and value != "na" else ""
    return data


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def with_procedure(model, checklist=CHECKLIST, code="HM-G5-PM6"):
    proc = PmProcedure.objects.create(code=code, name="Hamilton G5 6-month PM", checklist=checklist, revision="C")
    model.pm_procedure = proc
    model.save()
    return proc


# --- the modal --------------------------------------------------------------------------------------------------------------------


def test_the_drawer_offers_mark_completed_on_an_open_inspection(client, signed_in, waiting):
    _asset, wo = waiting
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert f'hx-get="/work-orders/{wo.number}/complete/" hx-target="#modal-card">Mark completed</button>' in body  # one step from open


def test_a_waiting_devices_inspection_shows_the_incoming_checklist_and_what_each_result_does(client, signed_in, waiting):
    asset, wo = waiting
    signed_in("technician")
    r = client.get(url(wo), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and f"<h2>Complete {wo.number}</h2>" in body and "Incoming inspection · Dana Whitfield" in body
    assert "Not started yet: completing it records it as started and completed today." in body
    assert "Checklist: Incoming inspection checklist</span>" in body and "this is the incoming checklist" in body
    assert [row["text"] for row in r.context["rows"]] == [text for text, _m in inspections.INCOMING_CHECKLIST]
    assert body.count('<input class="rd" type="text" name="reading_') == 1 and '<small class="muted">Record leakage µA</small>' in body  # never "Record Record"
    assert 'name="inspection_result" value="passed"' in body and 'name="inspection_result" value="failed"' in body
    assert "<span>Passed<small>Goes into service and its PMs start</small></span>" in body
    assert "<span>Failed<small>Stays out of service; a re-inspection opens</small></span>" in body
    assert 'id="wc-result-label">Result</div>' in body and "Result, optional" not in body
    assert "If it fails" in body and "A re-inspection is opened, due in 14 days, assigned to Dana Whitfield." in body
    assert "NEW-1 stays out of service until an inspection passes." in body and 'name="tag_out"' not in body  # nothing in use to take out
    assert 'name="pm_result"' not in body and "If the PM failed" not in body and "Why was it late?" not in body
    assert "Mark the rest pass" in body and "acceptance report" in body
    assert asset.awaiting_inspection


def test_an_inspection_uses_the_models_procedure_when_it_has_steps(client, signed_in, waiting, vent_model):
    _asset, wo = waiting
    with_procedure(vent_model)
    signed_in("technician")
    r = client.get(url(wo), **HX)
    body = r.content.decode()
    assert "<b>HM-G5-PM6</b> · Hamilton G5 6-month PM" in body and "this is the incoming checklist" not in body
    assert [row["text"] for row in r.context["rows"]] == ["Inspect and clean", "Leakage current", "Alarm check"]


def test_a_vendors_inspection_sends_its_reinspection_to_the_vendor(client, signed_in, ctx, today, vent_model):
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, vendor_name="Hamilton Medical field service")
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "A re-inspection is opened, due in 14 days, sent to Hamilton Medical field service." in body


def test_a_device_in_use_before_its_inspection_offers_the_tag_out(client, signed_in, ctx, today, vent_model, techs):
    asset = in_use_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, technician=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "<span>Passed<small>Stays in use and its PMs start</small></span>" in body
    assert "<span>Failed<small>A re-inspection opens</small></span>" in body
    assert ('name="tag_out" id="wc-tag_out" checked> Take NEW-4 out of service (it is in service before its inspection) until an '
            'inspection passes</label><input type="hidden" name="shown_tag_out" value="1">') in body


def test_an_inspection_of_a_device_not_waiting_takes_an_optional_result_that_moves_nothing(client, signed_in, ctx, vent, techs):
    wo = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect after the move to the new ICU")
    assign(wo, technician=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "Result, optional</div>" in body and "CE-10001 is not waiting for its incoming inspection, so the result changes nothing" in body
    assert "<span>Passed<small>Every check passed</small></span>" in body and "If it fails" not in body
    r = client.post(url(wo), {**steps_data(wo, ["", "", "", "", ""], reading=""), "resolution": "Checked after the move"}, **HX)
    wo.refresh_from_db()
    vent.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and triggers(r)["toast"]["value"] == f"{wo.number} completed"
    assert wo.status == WoStatus.COMPLETED and wo.inspection_result == "" and wo.checklist_results == []
    assert vent.status == AssetStatus.IN_SERVICE and not vent.awaiting_inspection
    assert "Inspection result" not in r.content.decode()  # no result and no steps: nothing to show
    # its steps without a result still show, as "No result recorded"
    other = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect again")
    assign(other, technician=techs["dana"])
    assert "Say what was found and done." in client.post(url(other), steps_data(other), **HX).content.decode()  # no result: say what
    body = client.post(url(other), {**steps_data(other), "resolution": "All checks done after the move"}, **HX).content.decode()
    assert "Inspection result <span>5 steps as recorded" in body and '<span class="chip neutral">No result recorded</span>' in body


# --- saving -----------------------------------------------------------------------------------------------------------------------


def test_a_pass_puts_the_device_in_service_and_answers_with_the_drawer(client, signed_in, waiting, today):
    asset, wo = waiting
    signed_in("technician")
    r = client.post(url(wo), {**steps_data(wo), "inspection_result": PASSED}, **HX)
    asset.refresh_from_db()
    wo.refresh_from_db()
    first = add_months(today, asset.pm_interval_months)
    assert asset.status == AssetStatus.IN_SERVICE and not asset.awaiting_inspection and asset.next_pm_on == first
    assert wo.status == WoStatus.COMPLETED and wo.inspection_result == PASSED and wo.started_on == today
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    events = triggers(r)
    assert events["toast"]["value"] == f"{wo.number} completed: Incoming inspection passed; NEW-1 in service, first PM due {day(first)}"
    assert "wo-changed" in events and "devices-changed" in events and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    body = r.content.decode()
    assert f"Inspection result <span>5 steps as recorded {day(today)}</span>" in body and '<span class="chip ok">Passed</span>' in body
    assert f'<p class="wr-by">Inspected by Dana Whitfield on {day(today)}</p>' in body
    assert f"<td>{LEAKAGE} <span class=\"muted\">· Record leakage µA</span></td>" in body
    assert "Incoming inspection passed per the incoming checklist, all checks passed" in body
    assert "PM result" not in body and "Status changed to completed: Incoming inspection passed" in body


def test_a_pass_says_when_an_open_tagged_out_repair_still_holds_the_device(client, signed_in, waiting, today, techs):
    asset, wo = waiting
    create_work_order(asset=asset, type=WoType.REPAIR, priority="normal", problem="Cracked display", tag_out=True, assigned_to=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "<span>Passed<small>Its PMs start; it stays out of service until its open repair is done</small></span>" in body
    r = client.post(url(wo), {**steps_data(wo), "inspection_result": PASSED}, **HX)
    asset.refresh_from_db()
    assert asset.status == AssetStatus.OUT_OF_SERVICE and not asset.awaiting_inspection
    assert triggers(r)["toast"]["value"] == (f"{wo.number} completed: Incoming inspection passed; NEW-1 stays out of service until its open "
                                             f"repair is done; first PM due {day(asset.next_pm_on)}")


def test_a_fail_opens_the_reinspection_and_the_two_link_both_ways(client, signed_in, waiting, today):
    asset, wo = waiting
    signed_in("technician")
    r = client.post(url(wo), {**steps_data(wo, ["pass", "fail", "pass", "pass", "pass"], reading="640"), "inspection_result": FAILED,
                              "resolution": "Leakage 640 µA, over the limit; vendor to swap the unit"}, **HX)
    again = WorkOrder.objects.get(follow_up_of=wo)
    asset.refresh_from_db()
    assert again.type == WoType.INSPECTION and again.status == WoStatus.OPEN and again.assigned_to.name == "Dana Whitfield"
    assert asset.status == AssetStatus.OUT_OF_SERVICE and asset.awaiting_inspection and asset.next_pm_on is None
    events = triggers(r)
    assert events["toast"]["value"] == f"{wo.number} completed: Incoming inspection failed; {again.number} opened to re-inspect"
    assert "devices-changed" not in events  # the device did not move
    body = r.content.decode()
    assert '<span class="chip crit">Failed</span>' in body and '<tr><td class="muted">2</td>' in body
    link = f'<a class="link" href="/work-orders/{again.number}/" hx-get="/work-orders/{again.number}/" hx-target="#drawer">{again.number}</a>'
    assert "Re-inspection <span>opened when this inspection failed</span>" in body and link in body
    back = client.get(f"/work-orders/{again.number}/", **HX).content.decode()
    link = f'<a class="link" href="/work-orders/{wo.number}/" hx-get="/work-orders/{wo.number}/" hx-target="#drawer">{wo.number}</a>'
    assert f"Opened from failed incoming inspection {link}, done {day(today)}." in back and "failed PM" not in back
    # the re-inspection's modal: one already open is not this one, so a second fail would open another
    body = client.get(url(again), **HX).content.decode()
    assert "A re-inspection is opened, due in 14 days, assigned to Dana Whitfield." in body


def test_errors_come_back_with_what_was_chosen(client, signed_in, waiting):
    _asset, wo = waiting
    signed_in("technician")
    r = client.post(url(wo), steps_data(wo, ["pass", "fail", "pass", "pass", "pass"]), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "toast" not in triggers(r)
    assert "Choose the inspection&#x27;s result: passed or failed." in body
    assert 'name="inspection_result" value="passed" autofocus' in body  # the first error on screen
    assert '<input type="radio" name="step_2" value="fail" aria-label="Step 2: Fail" checked>' in body
    r = client.post(url(wo), {**steps_data(wo, ["pass", "fail", "pass", "pass", "pass"]), "inspection_result": PASSED}, **HX)
    body = r.content.decode()
    assert "Step 2 failed. A device that fails a check fails its incoming inspection: choose Failed." in body
    assert 'name="inspection_result" value="passed" checked autofocus' in body
    r = client.post(url(wo), {**steps_data(wo, ["", "", "", "", ""], reading=""), "inspection_result": PASSED}, **HX)
    assert "Record the checklist, or say what was checked." in r.content.decode()
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN and wo.inspection_result == ""


def test_mark_the_rest_pass_keeps_the_result(client, signed_in, waiting):
    _asset, wo = waiting
    signed_in("technician")
    r = client.get(url(wo, fill="pass", step_2="fail", reading_2="640", inspection_result=FAILED), **HX)
    body = r.content.decode()
    assert [row["value"] for row in r.context["rows"]] == ["fail" if n == 2 else "pass" for n in range(1, 6)]
    assert 'name="inspection_result" value="failed" checked' in body and 'class="err"' not in body


def test_a_vendor_acceptance_report_passes_without_the_checklist(client, signed_in, ctx, today, vent_model):
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, vendor_name="Hamilton Medical field service")
    signed_in("technician")
    data = {**steps_data(wo, ["", "", "", "", ""], reading=""), "inspection_result": PASSED,
            "resolution": "Acceptance test per the vendor's report AT-2291, on file"}
    body = client.post(url(wo), data, **HX).content.decode()
    asset.refresh_from_db()
    assert asset.status == AssetStatus.IN_SERVICE and not asset.awaiting_inspection
    assert "Inspection result <span>no checklist recorded</span>" in body
    assert f'<p class="wr-by">Inspected by Hamilton Medical field service on {day(today)}</p>' in body


def test_a_device_in_use_that_fails_is_taken_out_unless_unticked(client, signed_in, ctx, today, vent_model, techs):
    signed_in("technician")
    fail = {"inspection_result": FAILED, "resolution": "Alarm does not sound"}
    kept = in_use_device(vent_model, "NEW-5", today=today)
    wo = inspections.open_inspection(kept)
    assign(wo, technician=techs["dana"])
    r = client.post(url(wo), {**steps_data(wo, ["pass", "pass", "fail", "pass", "pass"]), **fail, "shown_tag_out": "1"}, **HX)  # unticked
    kept.refresh_from_db()
    assert kept.status == AssetStatus.IN_SERVICE and "tagged out" not in triggers(r)["toast"]["value"]
    out = in_use_device(vent_model, "NEW-6", today=today)
    wo = inspections.open_inspection(out)
    assign(wo, technician=techs["dana"])
    r = client.post(url(wo), {**steps_data(wo, ["pass", "pass", "fail", "pass", "pass"]), **fail, "shown_tag_out": "1", "tag_out": "on"}, **HX)
    out.refresh_from_db()
    again = WorkOrder.objects.get(follow_up_of=wo)
    assert out.status == AssetStatus.OUT_OF_SERVICE and out.awaiting_inspection
    assert triggers(r)["toast"]["value"] == (f"{wo.number} completed: Incoming inspection failed; {again.number} opened to re-inspect; "
                                             "NEW-6 tagged out of service")
    assert "devices-changed" in triggers(r)


def test_a_pass_on_a_device_that_no_longer_waits_is_kept(client, signed_in, ctx, today, vent_model, techs):
    asset, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened
    signed_in("technician")
    drawer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert ("Reopened. When last completed this inspection was recorded as Passed, and NEW-2 no longer waits for its incoming "
            "inspection: completing it again keeps it passed.") in drawer
    body = client.get(url(wo), **HX).content.decode()
    assert 'name="inspection_result" value="passed" checked' in body and "Result, optional" in body
    assert f"{wo.number} passed and NEW-2 no longer waits for its incoming inspection, so it stays passed." in body
    assert "If it fails" not in body
    r = client.post(url(wo), {**steps_data(wo), "inspection_result": FAILED, "resolution": "Alarm fails"}, **HX)
    assert "tag the device out and open a repair" in r.content.decode()
    r = client.post(url(wo), steps_data(wo), **HX)  # blank keeps Passed
    wo.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and wo.inspection_result == PASSED and wo.status == WoStatus.COMPLETED


def test_a_reopened_failed_inspection_records_on_its_open_reinspection(client, signed_in, ctx, today, vent_model, techs):
    asset, wo, again = failed_device(vent_model, technician=techs["dana"], today=today)
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert f"Re-inspection {again.number} is already open: the failure is recorded on it." in body
    assert "<span>Failed<small>Stays out of service; recorded on the re-inspection open</small></span>" in body
    r = client.post(url(wo), {**steps_data(wo, ["pass", "fail", "pass", "pass", "pass"], reading="640"), "inspection_result": FAILED,
                              "resolution": "Still over the limit"}, **HX)
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Incoming inspection failed; re-inspection {again.number} already open"
    assert WorkOrder.objects.filter(follow_up_of=wo).count() == 1


# --- from My work -----------------------------------------------------------------------------------------------------------------


@pytest.fixture
def dana_user(signed_in, techs):
    """Dana signed in: a technician whose profile is the techs fixture's Dana."""
    user = signed_in("technician")
    techs["dana"].user = user
    techs["dana"].save()
    return user


def test_my_work_offers_complete_on_an_open_inspection_and_start_on_a_repair(client, dana_user, waiting, vent, techs):
    _asset, wo = waiting
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Alarm", assigned_to=techs["dana"])
    assert [a["label"] for a in views_my_work._actions(dana_user, wo)] == ["Complete", "Log time"]
    assert [a["label"] for a in views_my_work._actions(dana_user, repair)] == ["Start", "Log time"]
    body = client.get("/my-work/", **{**HX, "HTTP_HX_TARGET": "my-work-body"}).content.decode()
    card = re.search(r'<article class="mw-card[^"]*"[^>]*hx-get="/work-orders/%s/".*?</article>' % re.escape(wo.number), body, re.S).group(0)
    assert f'hx-get="/work-orders/{wo.number}/complete/"' in card and ">Start<" not in card


def test_from_my_work_a_pass_stays_on_the_list_and_a_fail_shows_its_drawer(client, dana_user, ctx, today, vent_model, techs):
    passing = waiting_device(vent_model, "NEW-7", today=today)
    wo = inspections.open_inspection(passing)
    assign(wo, technician=techs["dana"])
    r = client.post(url(wo), {**steps_data(wo), "inspection_result": PASSED, "from": "my_work"}, **HX)
    assert r["HX-Reswap"] == "none" and "HX-Retarget" not in r and "Incoming inspection passed" in triggers(r)["toast"]["value"]
    failing = waiting_device(vent_model, "NEW-8", today=today)
    wo = inspections.open_inspection(failing)
    assign(wo, technician=techs["dana"])
    r = client.post(url(wo), {**steps_data(wo, ["fail", "pass", "pass", "pass", "pass"]), "inspection_result": FAILED,
                              "resolution": "Cracked housing", "from": "my_work"}, **HX)
    assert r["HX-Retarget"] == "#drawer" and "Re-inspection <span>opened when this inspection failed</span>" in r.content.decode()


# --- a vendor's inspection --------------------------------------------------------------------------------------------------------


def test_a_vendor_completes_their_inspection_and_never_sees_another_number(client, signed_in, ctx, today, vent_model, techs):
    vendor = "Hamilton Medical field service"
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, vendor_name=vendor)
    signed_in("vendor", company="Hamilton Medical")
    assert client.get(url(wo), **HX).status_code == 200
    r = client.post(url(wo), {**steps_data(wo, ["pass", "fail", "pass", "pass", "pass"]), "inspection_result": FAILED,
                              "resolution": "Leakage over the limit"}, **HX)
    again = WorkOrder.objects.get(follow_up_of=wo)
    assert again.vendor_service and again.vendor_name == vendor  # the vendor's acceptance test is redone by them: theirs to see
    assert triggers(r)["toast"]["value"] == f"{wo.number} completed: Incoming inspection failed; {again.number} opened to re-inspect"
    assign(again, technician=techs["dana"])  # CE takes the re-inspection in house: no longer the vendor's
    change_status(wo, WoStatus.IN_PROGRESS)  # the vendor's failed inspection reopened
    body = client.get(url(wo), **HX).content.decode()
    assert "A re-inspection is already open: the failure is recorded on it." in body and again.number not in body
    r = client.post(url(wo), {**steps_data(wo, ["pass", "fail", "pass", "pass", "pass"]), "inspection_result": FAILED,
                              "resolution": "Still over the limit"}, **HX)
    message = triggers(r)["toast"]["value"]
    assert message == f"{wo.number} completed: Incoming inspection failed; a re-inspection was already open" and again.number not in message
    assert again.number not in r.content.decode()  # the drawer neither lists nor links it


def test_a_vendors_pass_puts_the_device_in_service(client, signed_in, ctx, today, vent_model):
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, vendor_name="Hamilton Medical field service")
    signed_in("vendor", company="Hamilton Medical")
    r = client.post(url(wo), {**steps_data(wo), "inspection_result": PASSED}, **HX)
    asset.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and asset.status == AssetStatus.IN_SERVICE and not asset.awaiting_inspection


# --- the print --------------------------------------------------------------------------------------------------------------------


def test_an_open_inspection_prints_the_incoming_checklist_to_tick(client, signed_in, waiting):
    _asset, wo = waiting
    signed_in("technician")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    section = body.split("<h2>Inspection checklist</h2>")[1].split("<h2>Labor")[0]
    assert "<p><b>Incoming inspection checklist</b></p>" in section and section.count('<td class="chk"><span class="box"></span></td>') == 5
    assert "Record leakage µA" in section and '<div class="insp-result"><b>Result</b>' in section
    assert "PM checklist" not in body and "Accepted for clinical use by" in body and "Returned to service by" not in body


def test_a_completed_inspection_prints_its_result_inspector_and_steps(client, signed_in, ctx, today, vent_model, techs):
    asset, wo, again = failed_device(vent_model, technician=techs["dana"], today=today)
    signed_in("analyst")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert '<span class="chip bad">Inspection result: Failed</span>' in body
    assert f"<b>Result: Failed</b> · inspected by Dana Whitfield · completed {today:%b} {today.day}, {today.year}" in body
    assert '<tr class="failed"><td class="n">2</td>' in body and '640 <span class="muted">(Record leakage µA)</span>' in body
    assert "Leakage 640 µA, over the limit; vendor to swap the unit" in body and '<span class="box"></span></td>' not in body
    assert f'Re-inspection opened when this inspection failed: <b class="mono">{again.number}</b> · Open' in body
    back = client.get(f"/print/work-orders/{again.number}/").content.decode()
    assert f'<dt>Opened from</dt><dd>Failed incoming inspection <span class="mono">{wo.number}</span>, done {day(today)}</dd>' in back


def test_a_passed_inspection_prints_the_procedure_on_file(client, signed_in, ctx, today, vent_model, techs):
    with_procedure(vent_model)
    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=PASSED, results=[{"result": "pass"}, {"result": "pass", "reading": "38"}, {"result": "pass"}])
    signed_in("technician")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert '<span class="chip">Inspection result: Passed</span>' in body
    assert "The checklist as it was when this inspection was done. The procedure on file now: HM-G5-PM6, revision C." in body
    assert "Incoming inspection passed per HM-G5-PM6, all checks passed" in body


# --- the Work orders page ---------------------------------------------------------------------------------------------------------


def test_managers_are_told_how_many_inspections_wait_for_a_technician(client, signed_in, ctx, today, vent_model, vent, techs):
    first = waiting_device(vent_model, "NEW-1", today=today)
    waiting_device(vent_model, "NEW-2", today=today)
    taken = waiting_device(vent_model, "NEW-3", today=today)
    assign(inspections.open_inspection(taken), technician=techs["dana"])  # assigned: not waiting for a technician
    create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect")  # the device does not wait for it
    signed_in("manager")
    body = client.get("/work-orders/").content.decode()
    assert ('2 incoming inspections are waiting for a technician. <a class="link" '
            'href="/work-orders/?type=inspection&amp;assigned=unassigned&amp;q=&amp;open=1">Show them</a>') in body
    assign(inspections.open_inspection(first), vendor_name="Hamilton Medical field service")
    assert "1 incoming inspection is waiting for a technician." in client.get("/work-orders/").content.decode()


@pytest.mark.parametrize("role", ["technician", "analyst"])
def test_the_note_is_for_work_orders_approve_only(client, signed_in, ctx, today, vent_model, role):
    waiting_device(vent_model, today=today)
    signed_in(role)
    assert "waiting for a technician" not in client.get("/work-orders/").content.decode()


def test_a_scoped_vendor_never_gets_the_note(client, signed_in, ctx, today, vent_model):
    waiting_device(vent_model, today=today)
    signed_in("vendor", company="Hamilton Medical")
    assert "waiting for a technician" not in client.get("/work-orders/").content.decode()


# --- New work order ---------------------------------------------------------------------------------------------------------------


def test_new_work_order_fills_in_the_device_and_type_from_the_address(client, signed_in, ctx, today, vent_model, vent):
    asset = waiting_device(vent_model, today=today)
    change_status(inspections.open_inspection(asset), WoStatus.CANCELLED)  # none open: the drawer offers "Open incoming inspection"
    signed_in("manager")
    r = client.get(f"/work-orders/new/?asset={asset.tag}&type=inspection", **HX)
    body = r.content.decode()
    assert r.context["asset"] == asset and '<option value="inspection" selected>Incoming inspection</option>' in body
    assert f"{inspections.INCOMING_PROBLEM}</textarea>" in body
    body = client.get(f"/work-orders/new/?asset={vent.tag}&type=inspection", **HX).content.decode()  # not waiting: no words put in its mouth
    assert '<option value="inspection" selected>' in body and inspections.INCOMING_PROBLEM not in body
    r = client.post("/work-orders/new/", {"asset": asset.tag, "type": "inspection", "priority": "normal", "problem": inspections.INCOMING_PROBLEM},
                    **HX)
    wo = inspections.open_inspection(asset)
    assert r["HX-Retarget"] == "#drawer" and wo is not None and triggers(r)["toast"]["value"] == f"{wo.number} created"


def test_new_work_order_shows_the_services_refusals_on_the_form(client, signed_in, ctx, today, vent_model):
    asset = waiting_device(vent_model, today=today)
    open_one = inspections.open_inspection(asset)
    signed_in("manager")
    before = WorkOrder.objects.count()
    r = client.post("/work-orders/new/", {"asset": asset.tag, "type": "pm", "priority": "normal", "problem": "Scheduled PM"}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "toast" not in triggers(r)
    assert ('<span class="err" role="alert">NEW-1 is waiting for its incoming inspection: its PMs start when it passes its incoming '
            "inspection.</span>") in body
    assert "Scheduled PM</textarea>" in body  # what was typed is kept
    r = client.post("/work-orders/new/", {"asset": asset.tag, "type": "inspection", "priority": "normal", "problem": "Inspect"}, **HX)
    assert f"NEW-1 already has its incoming inspection open: {open_one.number}." in r.content.decode()
    assert WorkOrder.objects.count() == before


def test_the_reinspection_offer_matches_what_the_completion_does(ctx, today, vent_model, techs):
    """The modal's hint and the save read the same rule (completion.reinspection_assignee); an inactive technician's re-inspection
    is left for a manager."""
    from apps.web.views_wo_complete import _inspection_offers

    asset = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(asset)
    assign(wo, technician=techs["dana"])
    wo.refresh_from_db()
    assert _inspection_offers(wo, None)["reinspection_tech"] == techs["dana"]
    techs["dana"].is_active = False
    techs["dana"].save()
    wo.refresh_from_db()
    offers = _inspection_offers(wo, None)
    assert offers["reinspection_tech"] is None and offers["reinspection_vendor"] == ""
    done = complete_work_order(wo, inspection_result=FAILED, resolution="Cracked housing")
    assert done.reinspection.assigned_to_id is None and not done.reinspection.vendor_service
    assert completion.reinspection_assignee(wo) == ("", None)
