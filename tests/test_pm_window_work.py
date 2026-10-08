"""
Slice 27, part A, the work: the PM completion window (apps.pm.windows) where PMs are done. A PM completed after its due date but inside
its window sets its device's next PM from its due date, not from the day it was done (never under the default window); "late" for the
reason and the completion modal is after the window, by the model's class today; a reason is set only on a PM that missed its window
and cleared on any PM (one recorded before the window widened); the drawer's Why late row, the modal, and the API in the window's words;
the import summary's PM history and the AEM evidence counted by the window. The default window changes none of it.
"""
import csv
import html
import io
import json
import re
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import timezone

from apps.core.history import _evidence_text
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import PmWindow as K
from apps.imports import services as imports
from apps.imports.models import ImportRun
from apps.pm import aem
from apps.pm import windows as W
from apps.pm.dates import add_months
from apps.pm.services import missed_pms
from apps.web import views_wo_late
from apps.workorders import completion
from apps.workorders.models import LateReason, Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, create_work_order, pm_schedule_anchor, set_late_reason

HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/work-orders/"
DUE = date(2026, 3, 10)


def day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="GE", model="B650", description="Patient monitor", category="Monitors",
                                      risk_class=RiskClass.MEDIUM, oem_pm_interval_months=3)


@pytest.fixture
def monitor(ctx, dept, monitor_model):
    return Asset.objects.create(tag="CE-30001", device_model=monitor_model, department=dept)


@pytest.fixture
def director(make_user):
    return make_user("director")


def window(by, **fields):
    """Set the facility's windows through Settings, as the Settings panel does."""
    return fac.update_settings(by=by, **fields)


def pm_on(asset, due: date, *, tech=None, opened: date | None = None, source=Source.PM_PLANNER):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled preventive maintenance", source=source,
                           opened_on=opened or due - timedelta(days=20), due_on=due)
    if tech is not None:
        assign(wo, technician=tech)
    return wo


def done(asset, due: date, on: date, tech) -> Asset:
    """Complete a PM due `due` on `on`; the device as it is afterwards."""
    completion.complete_work_order(pm_on(asset, due, tech=tech), pm_result="pass", today=on)
    asset.refresh_from_db()
    return asset


# --- the schedule anchor (workorders.services.pm_schedule_anchor) -----------------------------------------------------------------


def test_a_pm_done_late_inside_its_window_sets_the_next_pm_from_its_due_date(monitor, vent, techs, director):
    window(director, pm_window_other=K.NEXT_MONTH)  # medium and low: by the end of the month after; life support stays by the due date
    assert done(monitor, DUE, date(2026, 4, 20), techs["dana"]).next_pm_on == date(2026, 6, 10)  # inside: from Mar 10, not Apr 20
    assert monitor.last_pm_on == date(2026, 4, 20)  # the day it was done, as always
    assert done(monitor, DUE, date(2026, 5, 2), techs["dana"]).next_pm_on == date(2026, 8, 2)  # after its window (Apr 30): from the day done
    assert done(monitor, DUE, date(2026, 3, 8), techs["dana"]).next_pm_on == date(2026, 6, 8)  # by its due date: from the day done
    assert done(monitor, DUE, DUE, techs["dana"]).next_pm_on == date(2026, 6, 10)
    # life support is judged by its own group's window, the due date: ten days late is late, from the day done
    assert done(vent, DUE, date(2026, 3, 20), techs["dana"]).next_pm_on == date(2026, 9, 20)


def test_the_default_window_keeps_the_day_it_was_done(monitor, techs):
    assert W.windows().is_default
    assert done(monitor, DUE, date(2026, 4, 20), techs["dana"]).next_pm_on == date(2026, 7, 20)


def test_the_anchor_by_kind_and_class(monitor, vent, techs):
    late_monitor, late_vent = pm_on(monitor, DUE), pm_on(vent, DUE)
    mixed = W.Windows(high=W.Window(K.DAYS_AFTER, 14), other=W.Window(K.DUE_MONTH))
    assert pm_schedule_anchor(late_monitor, date(2026, 3, 31), mixed) == DUE  # its month
    assert pm_schedule_anchor(late_monitor, date(2026, 4, 1), mixed) == date(2026, 4, 1)
    assert pm_schedule_anchor(late_vent, date(2026, 3, 24), mixed) == DUE  # 14 days
    assert pm_schedule_anchor(late_vent, date(2026, 3, 25), mixed) == date(2026, 3, 25)
    assert pm_schedule_anchor(late_vent, date(2026, 3, 9), mixed) == date(2026, 3, 9)  # early: from the day done
    assert pm_schedule_anchor(late_vent, date(2026, 3, 24), W.DEFAULT) == date(2026, 3, 24)


# --- "late" by the window (completion.completes_late) -----------------------------------------------------------------------------


def test_completes_late_follows_the_window_and_the_class(monitor, vent):
    mixed = W.Windows(high=W.Window(), other=W.Window(K.DUE_MONTH))
    m, v = pm_on(monitor, DUE), pm_on(vent, DUE)
    assert not completion.completes_late(m, date(2026, 3, 31), mixed) and completion.completes_late(m, date(2026, 4, 1), mixed)
    assert completion.inside_window(m, date(2026, 3, 31), mixed) == date(2026, 3, 31)
    assert completion.inside_window(m, DUE, mixed) is None and completion.inside_window(m, date(2026, 4, 1), mixed) is None
    assert completion.completes_late(v, date(2026, 3, 11), mixed) and completion.inside_window(v, date(2026, 3, 11), mixed) is None
    assert completion.completes_late(m, date(2026, 3, 11), W.DEFAULT) and not completion.completes_late(m, DUE, W.DEFAULT)


def test_a_reason_inside_the_window_is_refused_in_its_words_and_after_it_recorded(monitor, techs, director):
    window(director, pm_window_other=K.DUE_MONTH)
    inside = pm_on(monitor, DUE, tech=techs["dana"])
    with pytest.raises(ValidationError) as e:
        completion.complete_work_order(inside, pm_result="pass", late_reason=LateReason.STAFFING, today=date(2026, 3, 25))
    assert e.value.message_dict == {"late_reason": [f"{inside.number} is done inside its on-time window (until Mar 31, 2026), so it has no reason "
                                                    "to record."]}
    inside.refresh_from_db()
    assert inside.status == WoStatus.OPEN
    completion.complete_work_order(inside, pm_result="pass", today=date(2026, 3, 25))  # on time: no reason
    assert inside.late_reason == "" and inside not in missed_pms(date(2026, 4, 2))
    late = pm_on(monitor, DUE, tech=techs["dana"])
    completion.complete_work_order(late, pm_result="pass", late_reason=LateReason.STAFFING, today=date(2026, 4, 2))
    late.refresh_from_db()
    assert late.late_reason == LateReason.STAFFING and late in missed_pms(date(2026, 4, 2))
    monitor.refresh_from_db()
    assert monitor.next_pm_on == date(2026, 7, 2)  # past its window: from the day it was done


def test_set_late_reason_sets_a_reason_only_past_the_window(monitor, director):
    window(director, pm_window_other=K.DUE_MONTH)
    pm = pm_on(monitor, DUE)
    with pytest.raises(ValidationError) as e:
        set_late_reason(pm, LateReason.STAFFING, today=date(2026, 3, 25))
    assert e.value.message_dict == {"late_reason": [f"{pm.number} is not a PM that missed its on-time window (by the end of the due month), so it "
                                                    "has no reason to record."]}
    set_late_reason(pm, LateReason.STAFFING, today=date(2026, 4, 1))
    pm.refresh_from_db()
    assert pm.late_reason == LateReason.STAFFING


def test_a_reason_recorded_before_the_window_widened_can_be_cleared_never_set(monitor, vent, director):
    pm = pm_on(monitor, DUE)
    set_late_reason(pm, LateReason.STAFFING, today=date(2026, 3, 20))  # by the due date: missed
    window(director, pm_window_other=K.DUE_MONTH)  # widened: on time until Mar 31
    with pytest.raises(ValidationError, match="missed its on-time window"):
        set_late_reason(pm, LateReason.SCHEDULING, today=date(2026, 3, 20))
    set_late_reason(pm, "", by=director, today=date(2026, 3, 20))
    pm.refresh_from_db()
    assert pm.late_reason == "" and pm.history.first().history_change_reason == "Why late cleared"
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", opened_on=date(2026, 3, 1),
                               due_on=date(2026, 3, 5))
    with pytest.raises(ValidationError, match="is not a PM that missed its due date"):
        set_late_reason(repair, "", today=date(2026, 3, 20))  # only a PM has a reason to clear


# --- the drawer's Why late row and the Mark completed modal (real today: a window in days) ----------------------------------------


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def fortnight(ctx, director):
    """Both groups: within 14 days after the due date."""
    return window(director, pm_window_high=K.DAYS_AFTER, pm_window_high_days=14, pm_window_other=K.DAYS_AFTER, pm_window_other_days=14)


def test_the_row_shows_only_once_a_pm_is_past_its_window(client, signed_in, vent, techs, fortnight):
    today = timezone.localdate()
    inside, past = pm_on(vent, today - timedelta(days=5), tech=techs["dana"]), pm_on(vent, today - timedelta(days=20), tech=techs["dana"])
    signed_in("technician")
    assert 'id="wo-late"' not in client.get(f"/work-orders/{inside.number}/", **HX).content.decode()  # past its due date, not late yet
    assert 'id="wo-late" class="wo-late flag"' in client.get(f"/work-orders/{past.number}/", **HX).content.decode()
    r = client.post(f"/work-orders/{inside.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX)
    body = r.content.decode()
    assert "is not a PM that missed its on-time window (within 14 days after the due date)" in body and "<select" not in body
    inside.refresh_from_db()
    assert inside.late_reason == ""


def test_a_stale_reason_is_shown_and_can_be_cleared_from_the_drawer(client, signed_in, vent, techs, director):
    today = timezone.localdate()
    pm = pm_on(vent, today - timedelta(days=5), tech=techs["dana"])
    set_late_reason(pm, LateReason.DEVICE_IN_USE, today=today)  # by the due date: missed
    window(director, pm_window_high=K.DAYS_AFTER, pm_window_high_days=14)  # widened: on time again
    signed_in("technician")
    body = client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    row = html.unescape(body.split('id="wo-late"')[1].split("</div>")[0])
    assert views_wo_late.STALE_NOTE in row and views_wo_late.STALE_CLEAR in row and "wo-late flag" not in body
    assert re.findall(r'<option value="([^"]*)"', row) == ["", "device_in_use"]  # only clearing it
    r = client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": ""}, **HX)
    pm.refresh_from_db()
    assert pm.late_reason == "" and 'id="wo-late"' not in r.content.decode()  # cleared: no row left
    assert json.loads(r["HX-Trigger"])["toast"]["value"] == f"Why {pm.number} was late: cleared, not recorded"
    signed_in("analyst")  # read only: the note without the clearing words
    set_late_reason(pm, LateReason.DEVICE_IN_USE, today=pm.due_on + timedelta(days=15))  # recorded later, when it had missed its window
    row = html.unescape(client.get(f"/work-orders/{pm.number}/", **HX).content.decode().split('id="wo-late"')[1].split("</div>")[0])
    assert views_wo_late.STALE_NOTE in row and views_wo_late.STALE_CLEAR not in row and "<select" not in row


def test_the_modal_asks_why_only_past_the_window_and_says_so_inside_it(client, signed_in, vent, techs, fortnight):
    today = timezone.localdate()
    inside_due, past_due = today - timedelta(days=5), today - timedelta(days=20)
    inside, past = pm_on(vent, inside_due, tech=techs["dana"]), pm_on(vent, past_due, tech=techs["dana"])
    signed_in("technician")
    body = client.get(f"/work-orders/{inside.number}/complete/", **HX).content.decode()
    assert 'name="late_reason"' not in body
    assert (f"Past its due date, {day(inside_due)}, but inside the PM policy's on-time window until {day(inside_due + timedelta(days=14))}: "
            "it counts as on time, and the next PM is set from the due date.") in body
    body = client.get(f"/work-orders/{past.number}/complete/", **HX).content.decode()
    assert 'name="late_reason"' in body and "inside the PM policy's on-time window" not in body
    assert (f"Due {day(past_due)}, 20 days ago; its on-time window (within 14 days after the due date) ended {day(past_due + timedelta(days=14))}. "
            "Optional, but the survey binder") in body
    r = client.post(f"/work-orders/{inside.number}/complete/", {"pm_result": "pass"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    vent.refresh_from_db()
    assert vent.next_pm_on == add_months(inside_due, 6)  # from its due date


def test_the_default_modal_is_unchanged(client, signed_in, vent, techs):
    today = timezone.localdate()
    pm = pm_on(vent, today - timedelta(days=5), tech=techs["dana"])
    signed_in("technician")
    body = client.get(f"/work-orders/{pm.number}/complete/", **HX).content.decode()
    assert f"Due {day(pm.due_on)}, 5 days ago. Optional" in body and "on-time window" not in body


def test_the_api_speaks_the_windows_words(client, signed_in, vent, techs, fortnight):
    today = timezone.localdate()
    inside = pm_on(vent, today - timedelta(days=5), tech=techs["dana"])
    signed_in("technician")
    r = client.post(f"{API}{inside.id}/late-reason/", {"late_reason": "staffing"}, content_type="application/json")
    assert r.status_code == 400 and "missed its on-time window (within 14 days after the due date)" in r.json()["late_reason"][0]
    r = client.post(f"{API}{inside.id}/transition/", {"status": "completed", "pm_result": "pass", "late_reason": "staffing"},
                    content_type="application/json")
    assert r.status_code == 400 and "inside its on-time window" in r.json()["late_reason"][0]
    r = client.post(f"{API}{inside.id}/transition/", {"status": "completed", "pm_result": "pass"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    vent.refresh_from_db()
    assert vent.next_pm_on == add_months(inside.due_on, 6)


# --- the import summary and the AEM evidence -------------------------------------------------------------------------------------


def _imported(user, *rows):
    """A work order history file through the check and the import, as Settings' Import data runs it."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["WO #", "Control No.", "Work Type", "Status", "Date Opened", "Date Due", "Date Completed"])
    writer.writerows(rows)
    run = imports.upload(user, "work_orders", "history.csv", buf.getvalue().encode())
    run = imports.confirm_columns(run, user, imports.mapping_of(run))
    for step in (lambda r: r, lambda r: imports.start_import(r, user)):
        run = imports.process(step(run), user)
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = imports.process(run, user)
    assert run.status == "imported"
    return run


def test_the_import_summary_counts_pm_history_by_the_window_and_the_class(ctx, director, pump, monitor):
    window(director, pm_window_high=K.DAYS_AFTER, pm_window_high_days=5)  # the pump is high risk; the monitor medium, by the due date
    run = _imported(director, ["1", "CE-10002", "PM", "Closed", "2023-07-01", "2023-07-31", "2023-08-03"],
                    ["2", "CE-30001", "PM", "Closed", "2023-07-01", "2023-07-31", "2023-08-03"])
    assert run.summary["totals"]["PM history"] == {"On time": "1", "Late": "1"}


@pytest.fixture
def history(ctx, dept, monitor_model, techs):
    """Four years of a monitor: two PMs completed this year, three and ten days after their due dates."""
    today = timezone.localdate()
    device = Asset.objects.create(tag="M-1", device_model=monitor_model, department=dept, installed_on=today - timedelta(days=4 * 366))
    for after, due in ((3, today - timedelta(days=100)), (10, today - timedelta(days=50))):
        wo = pm_on(device, due, tech=techs["dana"])
        WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=due + timedelta(days=after), started_on=due)
    return today


def test_aem_evidence_counts_pms_on_time_by_the_models_window_and_names_it(history, monitor_model, director):
    ev = aem.evidence(monitor_model, history)
    assert (ev["pm_completed"], ev["pm_on_time"], ev["pm_on_time_pct"], ev["pm_window"]) == (2, 0, 0, "By the due date")
    window(director, pm_window_high=K.DUE_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=5)  # the monitor's group: 5 days
    ev = aem.evidence(monitor_model, history)
    assert json.loads(json.dumps(ev)) == ev
    assert (ev["pm_completed"], ev["pm_on_time"], ev["pm_on_time_pct"], ev["pm_window"]) == (2, 1, 50, "Within 5 days after the due date")
    assert _evidence_text(ev).endswith("50% of PMs on time (within 5 days after the due date)")
    assert _evidence_text({k: v for k, v in ev.items() if k != "pm_window"}).endswith("50% of PMs on time")  # a case from before slice 27
    page = render_to_string("web/_aem_evidence.html", {"ev": ev})
    assert "PMs on time, 1 of 2 completed (within 5 days after the due date)" in page
