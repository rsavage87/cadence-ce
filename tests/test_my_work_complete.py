"""
Completing on a phone (slice 24, part B): a PM still open completes in one step (started as it is completed, both moves in the status
history, in one transaction, for someone who may make both; a repair still needs Start), the drawer's Mark completed on it, the
optional Hours box (a labor line for the default technician today by add_labor's rules, refused in the modal with nothing saved),
the modal opened from My work (no drawer after the save, except a failed PM's), and the controls a gloved hand needs (a label per
step choice, the number pad for a reading that is a number). The API's transition to completed uses the same service.
"""
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.credentials.models import Technician
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web.forms_wo_complete import reads_number
from apps.workorders import completion
from apps.workorders import permissions as wo_perms
from apps.workorders.completion import checklist_signature, complete_work_order
from apps.workorders.costs import add_labor
from apps.workorders.models import LaborLine, PmResult, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
CHECKLIST = ["Inspect and clean", {"text": "Leakage current", "measure": "µA, limit 100"}, {"text": "Battery run time", "measure": True}, "Alarm check"]
ALL_PASS = [{"result": "pass"}, {"result": "pass", "reading": "42"}, {"result": "pass", "reading": "95 min"}, {"result": "pass"}]


@pytest.fixture
def procedure(ctx, vent_model):
    proc = PmProcedure.objects.create(code="HM-G5-PM6", name="Hamilton G5 6-month PM", checklist=CHECKLIST, revision="C")
    vent_model.pm_procedure = proc
    vent_model.save()
    return proc


def open_wo(asset, *, type=WoType.PM, tech=None, vendor="", problem="Scheduled preventive maintenance"):
    """A work order as the planner or a request leaves it: open, assigned, never started."""
    wo = create_work_order(asset=asset, type=type, priority="normal", problem=problem, source=Source.PM_PLANNER if type == WoType.PM else Source.MANUAL)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    return wo


@pytest.fixture
def pm(procedure, vent, techs):
    return open_wo(vent, tech=techs["dana"])


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, *, profile=""):
        user = make_user(role_slug)
        if profile:
            Technician.objects.create(user=user, name=profile)
        client.force_login(user)
        return user

    return _as


def url(wo, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/work-orders/{wo.number}/complete/" + (f"?{query}" if query else "")


def post_data(steps=("pass", "pass", "pass", "pass"), readings=("", "42", "95 min", ""), **extra) -> dict:
    data = {"signature": checklist_signature(completion.checklist_of(PmProcedure.objects.get(code="HM-G5-PM6")))}
    for n, (step, reading) in enumerate(zip(steps, readings), 1):
        data[f"step_{n}"] = step
        data[f"reading_{n}"] = reading
    return {**data, **extra}


def moves(wo) -> list[tuple[str, str]]:
    return [(h.from_status, h.to_status) for h in wo.status_history.order_by("created_at", "id") if h.from_status != h.to_status]


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


# --- the service: a PM in one step ---------------------------------------------------------------------------------------------


def test_an_open_pm_is_started_and_completed_in_one_step(pm, vent):
    today = timezone.localdate()
    done = complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS)
    assert done.started and pm.status == WoStatus.COMPLETED and pm.started_on == today and pm.completed_on == today
    assert moves(pm) == [("", "open"), ("open", "in_progress"), ("in_progress", "completed")]  # the status history keeps both moves
    vent.refresh_from_db()
    assert vent.last_pm_on == today and done.device_changed


def test_a_pm_in_progress_is_not_started_again(pm):
    change_status(pm, WoStatus.IN_PROGRESS)
    done = complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS)
    assert not done.started and moves(pm).count(("open", "in_progress")) == 1


def test_a_repair_still_needs_start(ctx, vent, techs):
    repair = open_wo(vent, type=WoType.REPAIR, tech=techs["dana"], problem="Alarm")
    assert not completion.starts_on_completion(repair)
    with pytest.raises(ValidationError, match="has not been started. Start work on it first."):
        complete_work_order(repair, resolution="Fixed")
    repair.refresh_from_db()
    assert repair.status == WoStatus.OPEN and repair.started_on is None


def test_only_someone_who_may_start_it_completes_it_from_open(pm, make_user, monkeypatch):
    technician, manager = make_user("technician"), make_user("manager")
    assert completion.starts_on_completion(pm, technician) and completion.blocker(pm, by=technician) == ""
    # A facility where starting needed Approve: the technician completes it once someone has started it, never in one step.
    real = wo_perms.transition_level
    monkeypatch.setattr(wo_perms, "transition_level", lambda a, b: wo_perms.Level.APPROVE if (a, b) == (WoStatus.OPEN, WoStatus.IN_PROGRESS) else real(a, b))
    assert "has not been started" in completion.blocker(pm, by=technician) and completion.starts_on_completion(pm, manager)
    with pytest.raises(ValidationError, match="has not been started"):
        complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, by=technician)
    complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, by=manager)
    assert pm.status == WoStatus.COMPLETED


def test_an_unassigned_open_pm_is_not_completed(procedure, vent):
    wo = open_wo(vent)
    with pytest.raises(ValidationError, match="is not assigned"):
        complete_work_order(wo, pm_result=PmResult.PASS, results=ALL_PASS)
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN and moves(wo) == [("", "open")]


def test_a_refusal_leaves_the_pm_open_and_unstarted(pm, monkeypatch):
    with pytest.raises(ValidationError):
        complete_work_order(pm, pm_result="", results=ALL_PASS)  # no result: nothing is started
    pm.refresh_from_db()
    assert pm.status == WoStatus.OPEN and pm.started_on is None and moves(pm) == [("", "open")]

    def boom(*args, **kwargs):
        raise ValidationError("The repair could not be opened.")

    monkeypatch.setattr(completion, "_record_failure", boom)  # refused after the start: the start goes back too
    with pytest.raises(ValidationError):
        complete_work_order(pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, *ALL_PASS[1:]])
    pm.refresh_from_db()
    assert pm.status == WoStatus.OPEN and pm.started_on is None and moves(pm) == [("", "open")]


def test_a_failed_open_pm_opens_its_repair(pm, vent):
    done = complete_work_order(pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, *ALL_PASS[1:]])
    assert done.started and done.follow_up.follow_up_of == pm and done.follow_up.status == WoStatus.OPEN
    assert moves(pm)[-2:] == [("open", "in_progress"), ("in_progress", "completed")]
    vent.refresh_from_db()
    assert vent.status == "out_of_service"


# --- the service: hours ----------------------------------------------------------------------------------------------------------


def test_hours_are_logged_for_the_assigned_technician_today(pm, techs, make_user):
    by = make_user("technician")
    done = complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours="1.5", by=by)
    line = done.labor
    assert line.technician == techs["dana"] and line.hours == Decimal("1.5") and line.worked_on == timezone.localdate()
    assert line.rate == Decimal("82.00") and line.work_order_id == pm.pk  # the Settings rate


@pytest.mark.parametrize("hours", [None, "", "  "])
def test_no_hours_log_nothing(pm, hours):
    assert complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours=hours).labor is None
    assert not LaborLine.objects.exists()


def test_hours_over_a_day_are_refused_with_every_other_problem_and_nothing_is_saved(pm, techs, vent):
    other = open_wo(vent, type=WoType.REPAIR, tech=techs["dana"], problem="Alarm")
    add_labor(other, hours="20", worked_on=timezone.localdate(), by=None)
    with pytest.raises(ValidationError) as e:
        complete_work_order(pm, pm_result="", results=ALL_PASS, hours="5")
    errors = e.value.message_dict
    assert "pm_result" in errors and "already has 20 h logged" in " ".join(errors["hours"])  # reported at once
    with pytest.raises(ValidationError) as e:
        complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours="5")
    assert list(e.value.message_dict) == ["hours"]
    pm.refresh_from_db()
    assert pm.status == WoStatus.OPEN and pm.started_on is None and not pm.labor_lines.exists()  # not started, nothing logged
    assert complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours="4").labor.hours == Decimal("4")


def test_hours_that_are_not_a_number_are_refused(pm):
    with pytest.raises(ValidationError) as e:
        complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours="1,5")
    assert e.value.message_dict == {"hours": ["Enter a number."]}


def test_hours_with_nobody_to_credit_are_refused_in_words(pm, techs, make_user):
    Technician.objects.filter(pk=techs["dana"].pk).update(is_active=False)
    with pytest.raises(ValidationError) as e:
        complete_work_order(pm, pm_result=PmResult.PASS, results=ALL_PASS, hours="1", by=make_user("manager"))
    assert e.value.message_dict == {"hours": [completion.NO_TECHNICIAN]}


def test_vendor_hours_are_the_vendors_time(procedure, vent):
    wo = open_wo(vent, vendor="Hamilton Medical field service")
    line = complete_work_order(wo, pm_result=PmResult.PASS, results=ALL_PASS, hours="2").labor
    assert line.technician is None and line.rate == Decimal("215.00")


def test_a_repairs_hours_come_with_its_completion(ctx, vent, techs):
    repair = open_wo(vent, type=WoType.REPAIR, tech=techs["dana"], problem="Alarm")
    change_status(repair, WoStatus.IN_PROGRESS)
    done = complete_work_order(repair, resolution="Replaced the flow sensor", hours="0.75")
    assert done.labor.hours == Decimal("0.75") and repair.status == WoStatus.COMPLETED


# --- the drawer ------------------------------------------------------------------------------------------------------------------


def test_the_drawer_offers_mark_completed_on_an_open_pm(client, signed_in, pm, vent, techs):
    signed_in("technician")
    body = client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    footer = body.split('<div class="dr-f">')[1]
    assert f'<button class="btn primary" type="button" hx-get="/work-orders/{pm.number}/complete/" hx-target="#modal-card">Mark completed</button>' in footer
    assert '<button class="btn" type="button" hx-post' in footer and "Start work" in footer  # Start stays, for a PM over two visits
    repair = open_wo(vent, type=WoType.REPAIR, tech=techs["dana"], problem="Alarm")
    footer = client.get(f"/work-orders/{repair.number}/", **HX).content.decode().split('<div class="dr-f">')[1]
    assert "Mark completed" not in footer and "Start work" in footer
    signed_in("analyst")
    assert "Mark completed" not in client.get(f"/work-orders/{pm.number}/", **HX).content.decode()


def test_the_drawer_offers_nothing_on_an_unassigned_open_pm(client, signed_in, procedure, vent):
    wo = open_wo(vent)
    signed_in("technician")
    footer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode().split('<div class="dr-f">')[1]
    assert "Mark completed" not in footer and "Start work" not in footer


# --- the modal -------------------------------------------------------------------------------------------------------------------


def test_the_modal_completes_an_open_pm_and_answers_with_the_drawer(client, signed_in, pm):
    signed_in("technician")
    body = client.get(url(pm), **HX).content.decode()
    assert "Not started yet: completing it records it as started and completed today." in body and 'name="from"' not in body
    r = client.post(url(pm), post_data(pm_result="pass"), **HX)
    assert r["HX-Retarget"] == "#drawer" and "HX-Reswap" not in r and triggers(r)["toast"]["value"] == f"{pm.number} completed: PM passed"
    pm.refresh_from_db()
    assert pm.status == WoStatus.COMPLETED and moves(pm)[-2:] == [("open", "in_progress"), ("in_progress", "completed")]


def test_from_my_work_a_save_leaves_the_technician_on_my_work(client, signed_in, pm):
    signed_in("technician", profile="Dana Whitfield")
    body = client.get(url(pm, **{"from": "my_work"}), **HX).content.decode()
    assert '<input type="hidden" name="from" value="my_work">' in body
    r = client.post(url(pm), post_data(pm_result="pass", hours="1.5", **{"from": "my_work"}), **HX)
    assert r.status_code == 200 and r["HX-Reswap"] == "none" and "HX-Retarget" not in r and r.content == b""
    events = triggers(r)
    assert events["toast"]["value"] == f"{pm.number} completed: PM passed; 1.5 h logged" and "wo-changed" in events
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_from_my_work_a_failed_pm_still_shows_its_drawer(client, signed_in, pm):
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("fail", "pass", "pass", "pass"), pm_result="fail", shown_tag_out="1", tag_out="on",
                                       **{"from": "my_work"}), **HX)
    fu = WorkOrder.objects.get(follow_up_of=pm)
    assert r["HX-Retarget"] == "#drawer" and "HX-Reswap" not in r and fu.number in r.content.decode()  # the drawer names the repair
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_from_my_work_is_kept_through_errors_and_mark_the_rest_pass(client, signed_in, pm):
    signed_in("technician")
    r = client.post(url(pm), post_data(steps=("pass", "", "", ""), **{"from": "my_work"}), **HX)
    assert "Step 2: choose pass, fail, or N/A." in r.content.decode() and 'name="from" value="my_work"' in r.content.decode()
    r = client.get(url(pm), {"fill": "pass", "from": "my_work", "step_1": "fail"}, **HX)
    assert [row["value"] for row in r.context["rows"]] == ["fail", "pass", "pass", "pass"] and 'name="from" value="my_work"' in r.content.decode()


def test_the_hours_box(client, signed_in, pm):
    signed_in("technician")
    body = client.get(url(pm), **HX).content.decode()
    assert 'name="hours"' in body and 'inputmode="decimal"' in body and 'aria-describedby="wc-hours-for"' in body
    assert "Logged for Dana Whitfield today, at the Settings rate of $82.00 an hour." in body
    r = client.post(url(pm), post_data(pm_result="pass", hours="25"), **HX)
    body = r.content.decode()
    assert "HX-Retarget" not in r and "Log more than 0 and at most 24 hours on one line." in body and 'name="hours" value="25"' in body
    assert 'aria-describedby="wc-hours-for" autofocus aria-invalid="true" id="wc-hours">' in body  # the one error: focused
    pm.refresh_from_db()
    assert pm.status == WoStatus.OPEN and not pm.labor_lines.exists()


def test_no_hours_box_without_someone_to_log_them_for(client, signed_in, pm, techs):
    Technician.objects.filter(pk=techs["dana"].pk).update(is_active=False)
    signed_in("manager")  # no profile of their own
    body = client.get(url(pm), **HX).content.decode()
    assert 'name="hours"' not in body and "Logged for" not in body
    r = client.post(url(pm), post_data(pm_result="pass", hours="3"), **HX)  # a box not offered is not read
    assert r["HX-Retarget"] == "#drawer" and not LaborLine.objects.exists()


def test_a_vendors_hours_box_says_whose_time(client, signed_in, procedure, vent):
    wo = open_wo(vent, vendor="Hamilton Medical field service")
    user = signed_in("vendor")
    user.company = "Hamilton Medical"
    user.save()
    body = client.get(url(wo, **{"from": "my_work"}), **HX).content.decode()
    assert "Vendor time, Hamilton Medical field service today, at the Settings rate of $215.00 an hour." in body
    r = client.post(url(wo), post_data(pm_result="pass", hours="2", **{"from": "my_work"}), **HX)
    assert r["HX-Reswap"] == "none" and triggers(r)["toast"]["value"] == f"{wo.number} completed: PM passed; 2 h logged"
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.labor_lines.get().technician is None


def test_a_glove_can_choose_each_step(client, signed_in, pm):
    signed_in("technician")
    body = client.get(url(pm), **HX).content.decode()
    for n in range(1, 5):
        for value, label in (("pass", "Pass"), ("fail", "Fail"), ("na", "N/A")):
            assert (f'<label class="wc-pick"><input type="radio" name="step_{n}" value="{value}" aria-label="Step {n}: {label}">'
                    f'<span aria-hidden="true">{label}</span></label>') in body
    assert 'aria-label="Step 2: reading" placeholder="Reading" inputmode="decimal">' in body  # µA, limit 100: a number
    assert 'aria-label="Step 3: reading" placeholder="Reading">' in body  # a reading that says nothing of what: the keyboard
    assert 'class="btn sm wc-rest"' in body and "Mark the rest pass" in body
    assert 'class="modal-f wc-foot"' in body and 'class="modal-h wc-h"' in body


@pytest.mark.parametrize("measure, number", [
    ("µA, limit 100", True), ("leakage µA, limit 100", True), ("Ω", True), ("J delivered", True),
    ("serial number", False), ("firmware version", False), ("a description", False), (True, False), (None, False),
    ("°C, ±0.5 of the reference", False), ("mV, -10 to 10", False),  # below zero needs a minus: the keyboard
    # review fix: units a reading can go below zero in never bring up the number pad (it has no minus key), nor digits alone
    ("mmHg", False), ("SpO2 %", False), ("mmHg, 0-300", False), ("freezer temperature, °C", False), ("volume accuracy, %", False),
    ("software version 2.1.3a", False),
])
def test_which_readings_are_numbers(measure, number):
    assert reads_number(measure) is number


# --- the API ---------------------------------------------------------------------------------------------------------------------


def test_the_api_completes_an_open_pm_in_one_step(client, signed_in, pm):
    signed_in("technician")
    r = client.post(f"/api/v1/work-orders/{pm.id}/transition/", {"status": "completed", "pm_result": "pass", "results": ALL_PASS},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert moves(WorkOrder.objects.get(pk=pm.pk))[-2:] == [("open", "in_progress"), ("in_progress", "completed")]


# --- under the policies ------------------------------------------------------------------------------------------------------------


@needs_postgres
def test_completing_an_open_pm_from_my_work_under_the_policies(client, signed_in, pm, tenant):
    """As the runtime role: the start, the hours, and the completion are written in the facility the request set (the drawer's Mark
    completed reads the work order there too)."""
    signed_in("technician", profile="Dana Whitfield")
    data = post_data(pm_result="pass", hours="1.25", **{"from": "my_work"})
    as_app_role()
    assert "Mark completed" in client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    r = client.post(url(pm), data, **HX)
    assert r["HX-Reswap"] == "none" and triggers(r)["toast"]["value"] == f"{pm.number} completed: PM passed; 1.25 h logged"
    with tenant_context(tenant):  # the request restored the connection's facility setting when it finished
        pm.refresh_from_db()
        assert pm.status == WoStatus.COMPLETED and moves(pm)[-2:] == [("open", "in_progress"), ("in_progress", "completed")]
        assert pm.labor_lines.get().hours == Decimal("1.25")
