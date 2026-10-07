"""
Slice 26, the readers of incoming inspections (apps/reports/survey/inspections.py, inventory.py, maintenance.py, apps/reports/fleet.py):
the binder's evidence is the first inspection completed Passed (a blank result counts as passed), a failed one is listed with its
re-inspection and never the evidence; a new device in service with only failed inspections is a gap; one put in use before its
inspection is a finding, open or after it passed (when, why, who approved it); one entered as already in use with a recent install date
is a check; a new device retired without going into service counts as returned to the vendor; waiting devices count with their open
inspection; Result and Inspected by. A device waiting for its inspection has no next PM on purpose (never the inventory's gap), and
marked missing it is never past its PM date (the maintenance section, the PM compliance report).
"""
from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, incoming_results, waiting_device
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.credentials.models import Technician
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, RiskClass, UseBeforeInspection
from apps.reports.fleet import report_compliance
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, WORK_ORDER, inventory, maintenance
from apps.reports.survey import inspections as section
from apps.tenants.context import tenant_context
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult, Priority, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

S = AssetStatus
PASSED, FAILED = InspectionResult.PASSED, InspectionResult.FAILED
MARKER = "ZZ-REQUESTER-TEXT-ZZ"  # free text on the work orders and devices: never in the binder
LEAKAGE_FAIL = ("pass", "fail", "pass", "pass", "pass")


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def ago(today, days):
    return today - timedelta(days=days)


def _noon(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


def _day(d):
    return f"{d:%b} {d.day}, {d.year}"


def _new(dm, tag, today, days_ago, **extra):
    """A device added new `days_ago` days ago through Add device's waiting path (its added row and created_at on that day)."""
    a = waiting_device(dm, tag, today=today, added_on=ago(today, days_ago), **extra)
    Asset.objects.filter(pk=a.pk).update(created_at=_noon(ago(today, days_ago)), notes=MARKER)
    a.refresh_from_db()
    return a


def _added(tag, dm, dept, today, days_ago, added_as=AddedAs.NEW, status=S.IN_SERVICE, **extra):
    """A device added `days_ago` days ago in the status given (the API's old path for a new device; already in use; imported)."""
    a = eq.create_asset(tag=tag, device_model=dm, department=dept, status=status, added_as=added_as, added_on=ago(today, days_ago), today=today,
                        notes=MARKER, **extra)
    Asset.objects.filter(pk=a.pk).update(created_at=_noon(ago(today, days_ago)))
    return a


def _complete(asset, result, day, *, technician=None, vendor="", values=(), wo=None, **extra):
    """Complete the device's open incoming inspection (or `wo`) on `day` with `result`, the incoming checklist recorded."""
    wo = wo or inspections.open_inspection(asset)
    if technician is not None or vendor:
        assign(wo, technician=technician, vendor_name=vendor)
    results = incoming_results(*values) if values else INCOMING_ALL_PASS
    extra.setdefault("resolution", MARKER if result == FAILED else "")
    complete_work_order(wo, inspection_result=result, results=results, today=day, **extra)
    wo.refresh_from_db()
    return wo


def _figures(s):
    return {f.label: f.value for f in s.figures}


def _build(today, days=60):
    return section.build(period(ago(today, days), today), None)


# --- the evidence ---------------------------------------------------------------------------------------------------------------

def test_the_evidence_is_the_passed_inspection_and_a_failed_one_is_listed_never_counted(ctx, today, vent_model, techs):
    a = _new(vent_model, "N-1", today, 20)
    failed = _complete(a, FAILED, ago(today, 18), technician=techs["dana"], values=LEAKAGE_FAIL)
    again = failed.follow_ups.get()
    _complete(a, PASSED, ago(today, 10), wo=again)  # the vendor swapped the unit; the same technician re-inspected it
    s = _build(today)
    assert rows_of(s, "new_devices") == [["N-1", "Hamilton Medical Hamilton-G5", "Life support", ago(today, 20), "Awaiting inspection",
                                          again.number, "Completed", "Passed", "Dana Whitfield", ago(today, 10), ago(today, 10), None, ""]]
    failed_table = s.table("failed")
    assert failed_table.links == {0: WORK_ORDER, 1: DEVICE, 4: WORK_ORDER} and failed_table.count == 1
    assert rows_of(s, "failed") == [[failed.number, "N-1", ago(today, 18), "Dana Whitfield", again.number, "Completed", ago(today, 10)]]
    figures = _figures(s)
    assert figures["Inspected before first use"] == 1 and figures["Failed incoming inspections"] == 1
    assert figures["In service with no incoming inspection passed"] == 0
    assert s.gaps == []
    assert MARKER not in repr([r for t in s.tables for r in t.rows()]) + repr(s.gaps)


def test_a_failed_inspection_never_counts_and_a_device_in_service_with_only_failed_ones_is_a_gap(ctx, today, dept, vent_model, techs):
    """A device added new in service (the API's old path) whose only inspection failed: in service with none passed. A failed result
    on a device not waiting moves nothing and opens nothing."""
    a = _added("N-1", vent_model, dept, today, 12)
    wo = create_work_order(asset=a, type=WoType.INSPECTION, priority=Priority.NORMAL, problem=MARKER, opened_on=ago(today, 10),
                           assigned_to=techs["dana"])
    _complete(a, FAILED, ago(today, 8), wo=wo, values=LEAKAGE_FAIL)
    s = _build(today)
    assert [(g.kind, g.record, g.url) for g in s.gaps] == [(GAP, "N-1", "/equipment/N-1/")]
    assert s.gaps[0].text == (f"N-1 has been in service since {_day(ago(today, 12))} with no incoming inspection passed: {wo.number} failed on "
                              f"{_day(ago(today, 8))}.")
    assert rows_of(s, "new_devices")[0][5:10] == [wo.number, "Completed", "Failed", "Dana Whitfield", ago(today, 8)]
    assert rows_of(s, "failed") == [[wo.number, "N-1", ago(today, 8), "Dana Whitfield", "", "", None]]
    assert _figures(s)["In service with no incoming inspection passed"] == 1 and _figures(s)["Inspected before first use"] == 0


def test_a_blank_result_counts_as_passed_and_reads_not_recorded(ctx, today, dept, vent_model, techs):
    """An inspection completed with no result (before Cadence recorded one, or of a device not waiting for it) is the evidence."""
    a = _added("N-1", vent_model, dept, today, 12, status=S.OUT_OF_SERVICE)
    wo = create_work_order(asset=a, type=WoType.INSPECTION, priority=Priority.NORMAL, problem=MARKER, opened_on=ago(today, 12),
                           assigned_to=techs["dana"])
    complete_work_order(wo, resolution=MARKER, today=ago(today, 11))
    eq.set_status(Asset.objects.get(pk=a.pk), S.IN_SERVICE, changed_on=ago(today, 11), today=today)
    s = _build(today)
    assert rows_of(s, "new_devices")[0][5:12] == [wo.number, "Completed", "Not recorded", "Dana Whitfield", ago(today, 11), ago(today, 11), None]
    assert s.gaps == [] and _figures(s)["Inspected before first use"] == 1


def test_a_vendor_inspection_names_the_vendor(ctx, today, vent_model):
    a = _new(vent_model, "N-1", today, 6)
    _complete(a, PASSED, ago(today, 4), vendor="Hamilton Medical field service")
    assert rows_of(_build(today), "new_devices")[0][7:9] == ["Passed", "Hamilton Medical field service"]


# --- waiting, failed with nothing open, returned to the vendor -----------------------------------------------------------------

def test_waiting_devices_count_with_their_open_inspection(ctx, today, vent_model, pump_model, techs):
    waiting = _new(vent_model, "N-1", today, 3)
    failed = _new(pump_model, "N-2", today, 8)
    first = _complete(failed, FAILED, ago(today, 6), technician=techs["dana"], values=LEAKAGE_FAIL)
    again = first.follow_ups.get()
    s = _build(today)
    rows = {r[0]: r for r in rows_of(s, "new_devices")}
    assert rows["N-1"][4:10] == ["Awaiting inspection", inspections.open_inspection(waiting).number, "Open", "", "", None]
    assert rows["N-2"][5:10] == [again.number, "Open", "", "", None]  # its re-inspection open; the failed one is listed below
    assert rows_of(s, "failed") == [[first.number, "N-2", ago(today, 6), "Dana Whitfield", again.number, "Open", None]]
    figures = {f.label: (f.value, f.hint) for f in s.figures}
    assert figures["Waiting: never in service yet"] == (2, "2 with an incoming inspection open") and s.gaps == []


def test_a_waiting_device_that_failed_with_no_re_inspection_open_is_a_check(ctx, today, vent_model, techs):
    a = _new(vent_model, "N-1", today, 8)
    first = _complete(a, FAILED, ago(today, 6), technician=techs["dana"], values=LEAKAGE_FAIL)
    change_status(first.follow_ups.get(), WoStatus.CANCELLED)
    gone = _new(vent_model, "N-2", today, 5)
    change_status(inspections.open_inspection(gone), WoStatus.CANCELLED)  # nobody opened another
    s = _build(today)
    assert [(g.kind, g.record) for g in s.gaps] == [(CHECK, "N-1"), (CHECK, "N-2")]
    assert s.gaps[0].text == (f"N-1 failed its incoming inspection {first.number} on {_day(ago(today, 6))} and has waited out of service since, "
                              "with no re-inspection open.")
    assert s.gaps[1].text.startswith(f"N-2 has waited out of service since it was added on {_day(ago(today, 5))}, with no incoming inspection")
    assert rows_of(s, "new_devices")[0][5:8] == [first.number, "Completed", "Failed"]  # the last failed one, with nothing else


def test_a_new_device_retired_without_going_into_service_is_returned_to_the_vendor(ctx, today, vent_model, techs):
    a = _new(vent_model, "N-1", today, 20)
    first = _complete(a, FAILED, ago(today, 18), technician=techs["dana"], values=LEAKAGE_FAIL)
    eq.set_status(Asset.objects.get(pk=a.pk), S.RETIRED, today=today)  # back to the vendor: its re-inspection is cancelled
    b = _new(vent_model, "N-2", today, 4)
    eq.set_status(b, S.RETIRED, today=today)
    s = _build(today)
    figures = _figures(s)
    assert (figures["Returned to the vendor"], figures["Waiting: never in service yet"], figures["New devices added"]) == (2, 0, 2)
    assert s.gaps == [] and [r[0] for r in rows_of(s, "new_devices")] == ["N-1", "N-2"]
    again = first.follow_ups.get()
    assert rows_of(s, "failed") == [[first.number, "N-1", ago(today, 18), "Dana Whitfield", again.number, "Cancelled", None]]
    assert rows_of(s, "new_devices")[1][5:7] == ["", "None recorded"]


# --- in use before its incoming inspection -------------------------------------------------------------------------------------

def test_a_device_put_in_use_before_its_inspection_is_a_finding_open_or_after_it_passed(ctx, today, vent_model, pump_model, techs, make_user):
    director = make_user("director")
    still = _new(vent_model, "N-1", today, 5)
    eq.use_before_inspection(still, UseBeforeInspection.EMERGENCY, by=director, today=ago(today, 3))
    due = inspections.open_inspection(still)
    passed = _new(pump_model, "N-2", today, 9)
    eq.use_before_inspection(passed, UseBeforeInspection.LOANER_RENTAL, by=director, today=ago(today, 7))
    passed_wo = _complete(passed, PASSED, ago(today, 5), technician=techs["dana"])
    same = _new(pump_model, "N-3", today, 2)
    eq.use_before_inspection(same, UseBeforeInspection.ARRIVED_IN_USE, today=ago(today, 1))  # no approver recorded (a service call)
    same_wo = _complete(same, PASSED, ago(today, 1), technician=techs["dana"])
    s = _build(today)
    assert [(g.kind, g.record, g.url) for g in s.gaps] == [(FINDING, "N-2", "/equipment/N-2/"), (FINDING, "N-1", "/equipment/N-1/"),
                                                           (FINDING, "N-3", "/equipment/N-3/")]
    by_tag = {g.record: g.text for g in s.gaps}
    assert by_tag["N-2"] == (f"N-2 was in use 2 days before its incoming inspection: Loaner or rental needed now (approved by Director User; in use "
                             f"from {_day(ago(today, 7))}; {passed_wo.number} passed on {_day(ago(today, 5))}).")
    assert by_tag["N-1"] == (f"N-1 has been in use 3 days without its incoming inspection: Emergency clinical need (approved by Director User; in "
                             f"use from {_day(ago(today, 3))}; {due.number} is open, due {_day(ago(today, 2))}).")
    assert by_tag["N-3"] == (f"N-3 was in use less than a day before its incoming inspection: Arrived on the unit already in use (in use from "
                             f"{_day(ago(today, 1))}; {same_wo.number} passed on {_day(ago(today, 1))}).")
    rows = {r[0]: r for r in rows_of(s, "new_devices")}
    assert rows["N-2"][7:] == ["Passed", "Dana Whitfield", ago(today, 5), ago(today, 7), 2, "Loaner or rental needed now"]
    assert rows["N-1"][5:] == [due.number, "Open", "", "", None, ago(today, 3), None, "Emergency clinical need"]
    figures = _figures(s)
    assert figures["In use before its incoming inspection"] == 3
    assert (figures["Inspected after first use"], figures["In service with no incoming inspection passed"], figures["Inspected before first use"]) == (0, 0, 0)


def test_a_device_put_in_use_that_failed_and_was_taken_out_stays_a_finding(ctx, today, vent_model, techs, make_user):
    a = _new(vent_model, "N-1", today, 8)
    eq.use_before_inspection(a, UseBeforeInspection.EMERGENCY, by=make_user("director"), today=ago(today, 6))
    first = _complete(a, FAILED, ago(today, 4), technician=techs["dana"], values=LEAKAGE_FAIL)  # tagged out (the default)
    again = first.follow_ups.get()
    assert Asset.objects.get(pk=a.pk).status == S.OUT_OF_SERVICE
    s = _build(today)
    assert [(g.kind, g.record) for g in s.gaps] == [(FINDING, "N-1")]
    assert s.gaps[0].text == (f"N-1 was put in use before its incoming inspection on {_day(ago(today, 6))}: Emergency clinical need (approved by "
                              f"Director User; {again.number} is open, due {_day(again.due_on)}).")
    assert rows_of(s, "failed")[0][:5] == [first.number, "N-1", ago(today, 4), "Dana Whitfield", again.number]
    change_status(again, WoStatus.CANCELLED)
    assert _build(today).gaps[0].text.endswith(f"Director User; {first.number} failed on {_day(ago(today, 4))}, and no inspection is open).")


def test_only_a_listed_reason_is_ever_printed(ctx, today, dept, vent_model):
    """A device history row reads like a use before inspection only through use_before_inspection, which takes a listed reason; a
    status change's typed note (the API's) that starts the same way never puts its text in the binder."""
    a = _added("N-1", vent_model, dept, today, 5)
    eq.set_status(a, S.OUT_OF_SERVICE, note=f"{inspections.USE_BEFORE_PREFIX}{MARKER}", today=today)
    s = _build(today)
    assert MARKER not in repr(s.gaps) + repr([r for t in s.tables for r in t.rows()])
    assert all(section.UNLISTED_REASON in g.text for g in s.gaps if g.kind == FINDING)


# --- entered as already in use, installed recently -------------------------------------------------------------------------------

def test_a_device_entered_as_in_use_with_a_recent_install_date_is_a_check(ctx, today, dept, pump_model):
    existing = AddedAs.EXISTING
    _added("E-1", pump_model, dept, today, 5, added_as=existing, installed_on=ago(today, 15))  # 10 days before it was added
    _added("E-2", pump_model, dept, today, 5, added_as=existing, installed_on=ago(today, 36))  # 31 days: an older device
    _added("E-3", pump_model, dept, today, 5, added_as=existing, installed_on=ago(today, 5))  # the same day
    _added("E-4", pump_model, dept, today, 5, added_as=existing, installed_on=ago(today, 3))  # after it was added
    _added("E-5", pump_model, dept, today, 5, added_as=existing, installed_on=ago(today, 35))  # 30 days: still recent
    _added("E-6", pump_model, dept, today, 5, added_as=existing)  # no install date: nothing to go on
    _added("I-1", pump_model, dept, today, 5, added_as=AddedAs.IMPORTED, installed_on=ago(today, 6))  # imported: never asked
    _added("OLD-1", pump_model, dept, today, 90, added_as=existing, installed_on=ago(today, 91))  # before the period
    s = _build(today)
    assert [(g.kind, g.record, g.url) for g in s.gaps] == [(CHECK, "E-1", "/equipment/E-1/"), (CHECK, "E-3", "/equipment/E-3/"),
                                                           (CHECK, "E-4", "/equipment/E-4/"), (CHECK, "E-5", "/equipment/E-5/")]
    added = _day(ago(today, 5))
    assert [g.text for g in s.gaps] == [
        f"E-1 was added as already in use on {added} but installed 10 days before: was it new? A new device is inspected before first use.",
        f"E-3 was added as already in use on {added} but installed the same day: was it new? A new device is inspected before first use.",
        f"E-4 was added as already in use on {added} but installed 2 days after it: was it new? A new device is inspected before first use.",
        f"E-5 was added as already in use on {added} but installed 30 days before: was it new? A new device is inspected before first use."]
    figure = next(f for f in s.figures if f.label == "Entered as already in use")
    assert (figure.value, figure.hint) == (6, "not asked for an incoming inspection; 4 installed within 30 days before they were added")
    assert rows_of(s, "new_devices") == []


# --- the inventory, the maintenance section, and the PM compliance report --------------------------------------------------------

@pytest.fixture
def awaiting(ctx, today, vent_model, techs):
    """Three ventilators waiting for their inspection: out of service, marked missing, and in use before it."""
    out = _new(vent_model, "W-1", today, 3)
    lost = _new(vent_model, "W-2", today, 3)
    eq.set_status(lost, S.MISSING, today=today)
    used = _new(vent_model, "W-3", today, 3)
    eq.use_before_inspection(used, UseBeforeInspection.EMERGENCY, today=today)
    return {"out": out, "lost": lost, "used": used}


def test_the_inventory_never_asks_a_waiting_device_out_of_use_for_a_next_pm(ctx, today, dept, vent_model, awaiting):
    Asset.objects.create(tag="V-1", device_model=vent_model, department=dept, next_pm_on=None)  # on no PM schedule by mistake
    s = inventory.build(period(ago(today, 30), today), None)
    # W-1 and W-2 wait out of use, so they are not asked for one; W-3, in use before its inspection, is on a patient with no PM
    # schedule, so it is listed (review fix), in words that send the reader to its inspection
    assert [(g.kind, g.record) for g in gaps_of(s, GAP)] == [(GAP, "V-1"), (GAP, "W-3")]
    assert "in use before its incoming inspection" in gaps_of(s, GAP)[1].text
    assert [g.record for g in gaps_of(s, FINDING)] == ["W-2"]  # a missing life-support device is still a missing one
    rows = {r[0]: r for r in rows_of(s, "devices")}
    assert [(rows[t][8], rows[t][12]) for t in ("W-1", "W-2", "W-3")] == [("Awaiting inspection", None), ("Missing", None), ("In service", None)]
    assert _figures(s)["Active devices"] == 4 and any("waiting for its incoming inspection" in n for n in s.notes)


def test_a_missing_waiting_device_is_never_past_its_pm_date(ctx, today, dept, vent_model, awaiting):
    lost = Asset.objects.create(tag="V-1", device_model=vent_model, department=dept, next_pm_on=today + timedelta(days=60))
    eq.set_status(lost, S.MISSING, today=today)  # an ordinary missing device: past its PM date until it is found
    s = maintenance.build(period(ago(today, 30), today), None)
    assert [r[0] for r in rows_of(s, "past_pm_date")] == ["V-1"]
    assert [(g.kind, g.record) for g in gaps_of(s, GAP)] == [(GAP, "V-1")]
    assert rows_of(s, "by_class")[0][-1] == 1
    classes = {c["key"]: c for c in report_compliance(today)["classes"]}
    assert (classes["life_support"]["devices"], classes["life_support"]["overdue"]) == (4, 1)


# --- under row-level security ---------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_section_reads_inspections_and_uses_before_under_row_level_security(tenant, other_tenant, make_user):
    """As the runtime role: the inspections, their technicians, the devices' history, and who approved a use before inspection are
    read inside the facility only."""
    director = make_user("director")
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ED")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="C", risk_class=RiskClass.HIGH)
        eq.use_before_inspection(waiting_device(theirs, "THEIRS-1", department=their_dept), UseBeforeInspection.EMERGENCY)
    with tenant_context(tenant):
        today = timezone.localdate()
        dm = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defibrillator", category="Defibrillators",
                                        risk_class=RiskClass.LIFE_SUPPORT)
        tech = Technician.objects.create(name="Dana Whitfield")
        a = _new(dm, "N-1", today, 4)
        eq.use_before_inspection(a, UseBeforeInspection.EMERGENCY, by=director, today=ago(today, 3))
        wo = _complete(a, PASSED, ago(today, 2), technician=tech)
    as_app_role()
    with tenant_context(tenant):
        s = _build(today, 10)
        assert [r[0] for r in rows_of(s, "new_devices")] == ["N-1"]
        assert rows_of(s, "new_devices")[0][7:] == ["Passed", "Dana Whitfield", ago(today, 2), ago(today, 3), 1, "Emergency clinical need"]
        assert [(g.kind, g.record) for g in s.gaps] == [(FINDING, "N-1")]
        assert f"approved by Director User; in use from {_day(ago(today, 3))}; {wo.number} passed" in s.gaps[0].text
