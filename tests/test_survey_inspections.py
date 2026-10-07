"""
The survey binder's incoming inspection section (slice 25): the devices added as new in the period, each with its incoming inspection
and first day in service; a new device in service with no inspection is a gap, one inspected after it went into service a finding, one
waiting with no inspection open a check; devices entered as already in use, imported, or added before Cadence recorded how are only
counted. Slice 26's rules (the result, failed inspections, use before inspection, the recent install check) are in
tests/test_incoming_binder.py; here, the slice 25 records (no result recorded) still read as they did, and the query count holds with
all of it.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from incoming_fixtures import incoming_results
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, RiskClass, UseBeforeInspection
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, WORK_ORDER
from apps.reports.survey import inspections as section
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, create_work_order

MARKER = "ZZ-REQUESTER-TEXT-ZZ"


def _noon(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


def _day(d):
    return f"{d:%b} {d.day}, {d.year}"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def ago(today, days):
    return today - timedelta(days=days)


def _add(tag, dm, dept, today, days_ago, status=AssetStatus.OUT_OF_SERVICE, added_as=AddedAs.NEW):
    """A device added `days_ago` days ago, as Add device adds it (its added row and created_at on that day)."""
    a = eq.create_asset(tag=tag, device_model=dm, department=dept, status=status, added_as=added_as, added_on=ago(today, days_ago), today=today,
                        notes=MARKER)
    Asset.objects.filter(pk=a.pk).update(created_at=_noon(ago(today, days_ago)))
    return a


def _in_service(asset, today, days_ago):
    eq.set_status(Asset.objects.get(pk=asset.pk), AssetStatus.IN_SERVICE, changed_on=ago(today, days_ago), today=today)


def _inspection(asset, today, opened_ago, done_ago=None, status=None):
    wo = create_work_order(asset=asset, type=WoType.INSPECTION, priority=Priority.NORMAL, problem=MARKER, requester=MARKER,
                           opened_on=ago(today, opened_ago))
    if done_ago is not None:
        WorkOrder.objects.filter(pk=wo.pk).update(status=status or WoStatus.COMPLETED, completed_on=ago(today, done_ago), resolution=MARKER)
    elif status:
        WorkOrder.objects.filter(pk=wo.pk).update(status=status)
    wo.refresh_from_db()
    return wo


def _figures(s):
    return {f.label: f.value for f in s.figures}


def test_new_devices_with_their_inspection_and_first_day_in_service(ctx, today, dept, vent_model, pump_model):
    ok = _add("N-1", vent_model, dept, today, 20)
    ok_wo = _inspection(ok, today, 20, done_ago=15, status=WoStatus.CLOSED)
    _in_service(ok, today, 15)  # inspected the day it went into service: before first use
    _add("N-2", pump_model, dept, today, 12, status=AssetStatus.IN_SERVICE)  # in use from the day it was added, never inspected
    late = _add("N-3", pump_model, dept, today, 10, status=AssetStatus.IN_SERVICE)
    late_wo = _inspection(late, today, 6, done_ago=5)
    _inspection(late, today, 9, status=WoStatus.CANCELLED)  # never done
    _add("N-4", pump_model, dept, today, 8)  # waiting, and nobody has opened its inspection
    waiting = _add("N-5", vent_model, dept, today, 4)
    open_wo = _inspection(waiting, today, 4)
    inspected = _add("N-6", vent_model, dept, today, 3)
    inspected_wo = _inspection(inspected, today, 3, done_ago=2)  # inspected, not yet in use
    gone = _add("N-7", pump_model, dept, today, 30, status=AssetStatus.IN_SERVICE)
    eq.set_status(Asset.objects.get(pk=gone.pk), AssetStatus.OUT_OF_SERVICE, changed_on=ago(today, 25), today=today)
    _add("E-1", pump_model, dept, today, 10, status=AssetStatus.IN_SERVICE, added_as=AddedAs.EXISTING)
    _add("I-1", pump_model, dept, today, 10, status=AssetStatus.IN_SERVICE, added_as=AddedAs.IMPORTED)
    Asset.objects.create(tag="L-1", device_model=pump_model, department=dept)  # written without the services: blank
    _add("OLD-1", pump_model, dept, today, 90, status=AssetStatus.IN_SERVICE)  # before the period
    s = section.build(period(ago(today, 60), today), None)
    table = s.table("new_devices")
    assert table.links == {0: DEVICE, 5: WORK_ORDER} and table.count == 7
    # Slice 26: these inspections were completed with no result recorded (as before Cadence recorded one): each counts as passed.
    assert rows_of(s, "new_devices") == [
        ["N-7", "BD Alaris 8015 PCU", "High", ago(today, 30), "In service", "", "None recorded", "", "", None, ago(today, 30), None, ""],
        ["N-1", "Hamilton Medical Hamilton-G5", "Life support", ago(today, 20), "Out of service", ok_wo.number, "Closed", "Not recorded", "",
         ago(today, 15), ago(today, 15), None, ""],
        ["N-2", "BD Alaris 8015 PCU", "High", ago(today, 12), "In service", "", "None recorded", "", "", None, ago(today, 12), None, ""],
        ["N-3", "BD Alaris 8015 PCU", "High", ago(today, 10), "In service", late_wo.number, "Completed", "Not recorded", "", ago(today, 5),
         ago(today, 10), 5, ""],
        ["N-4", "BD Alaris 8015 PCU", "High", ago(today, 8), "Out of service", "", "None recorded", "", "", None, None, None, ""],
        ["N-5", "Hamilton Medical Hamilton-G5", "Life support", ago(today, 4), "Out of service", open_wo.number, "Open", "", "", None, None, None, ""],
        ["N-6", "Hamilton Medical Hamilton-G5", "Life support", ago(today, 3), "Out of service", inspected_wo.number, "Completed", "Not recorded", "",
         ago(today, 2), None, None, ""],
    ]
    assert rows_of(s, "failed") == []
    assert [(g.kind, g.record, g.url) for g in s.gaps] == [
        (GAP, "N-7", "/equipment/N-7/"), (GAP, "N-2", "/equipment/N-2/"), (FINDING, late_wo.number, f"/work-orders/{late_wo.number}/"),
        (CHECK, "N-4", "/equipment/N-4/")]
    assert gaps_of(s, GAP)[0].text == f"N-7 went into service on {_day(ago(today, 30))} with no incoming inspection recorded."
    assert gaps_of(s, GAP)[1].text == f"N-2 has been in service since {_day(ago(today, 12))} with no incoming inspection recorded."
    assert gaps_of(s, FINDING)[0].text.startswith("N-3 was inspected 5 days after it went into service")
    assert gaps_of(s, CHECK)[0].text.startswith(f"N-4 has waited out of service since it was added on {_day(ago(today, 8))}")
    assert _figures(s) == {"New devices added": 7, "Inspected before first use": 1, "Waiting: never in service yet": 3, "Returned to the vendor": 0,
                           "Inspected after first use": 1, "In use before its incoming inspection": 0, "In service with no incoming inspection passed": 2,
                           "Failed incoming inspections": 0, "Entered as already in use": 1, "Imported from the previous system": 1,
                           "Added before Cadence recorded how": 1}
    assert MARKER not in repr(rows_of(s, "new_devices")) + repr(s.gaps)


def test_devices_entered_as_in_use_imported_or_unrecorded_are_never_gaps(ctx, today, dept, pump_model):
    for tag, how in (("E-1", AddedAs.EXISTING), ("I-1", AddedAs.IMPORTED)):
        _add(tag, pump_model, dept, today, 5, status=AssetStatus.IN_SERVICE, added_as=how)
    Asset.objects.create(tag="L-1", device_model=pump_model, department=dept)
    s = section.build(period(ago(today, 30), today), None)
    assert s.gaps == [] and rows_of(s, "new_devices") == [] and _figures(s)["New devices added"] == 0


def test_the_added_day_is_the_facilitys(db):
    """Kona (UTC-10): a device added at 23:30 there on the day before the period is outside it, though it is the next day in UTC; one
    added at 00:30 on the period's first day is inside it."""
    kona = Tenant.objects.create(name="Kona Community", slug="kona", timezone="Pacific/Honolulu")
    zone = ZoneInfo("Pacific/Honolulu")
    with tenant_context(kona):
        today = timezone.localdate()
        start = ago(today, 10)
        dept = Department.objects.create(name="ED")
        dm = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defibrillator", category="Defibrillators",
                                        risk_class=RiskClass.LIFE_SUPPORT)
        for tag, moment in (("EVE", datetime.combine(ago(start, 1), time(23, 30), zone)), ("DAWN", datetime.combine(start, time(0, 30), zone))):
            a = eq.create_asset(tag=tag, device_model=dm, department=dept, status=AssetStatus.IN_SERVICE, today=today)
            Asset.objects.filter(pk=a.pk).update(created_at=moment)
            Asset.history.filter(id=a.pk).update(history_date=moment)
        s = section.build(period(start, today), None)
        assert [(r[0], r[3], r[10]) for r in rows_of(s, "new_devices")] == [("DAWN", start, start)]
        assert [g.record for g in s.gaps] == ["DAWN"]


def test_another_facilitys_devices_and_history_are_never_read(ctx, today, dept, pump_model, other_tenant):
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ED")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="C", risk_class=RiskClass.HIGH)
        eq.create_asset(tag="THEIRS-1", device_model=theirs, department=their_dept, status=AssetStatus.IN_SERVICE)
        eq.create_asset(tag="THEIRS-2", device_model=theirs, department=their_dept, added_as=AddedAs.EXISTING)
    mine = _add("N-1", pump_model, dept, today, 2)
    _inspection(mine, today, 2)
    s = section.build(period(ago(today, 30), today), None)
    assert [r[0] for r in rows_of(s, "new_devices")] == ["N-1"] and s.gaps == []
    assert _figures(s)["New devices added"] == 1 and _figures(s)["Entered as already in use"] == 0


def _devices(first, n, dm, dept, today, tech):
    """New devices in every state, and some entered as already in use with a recent install date (slice 26: the check). Of the new
    ones, some wait for their inspection through Add device's path; some of those are put in use before it, and some of those fail it."""
    for i in range(first, first + n):
        days = 1 + i % 20
        if i % 5 == 0:
            _add(f"E-{i:03d}", dm, dept, today, days, status=AssetStatus.IN_SERVICE, added_as=AddedAs.EXISTING)
            Asset.objects.filter(tag=f"E-{i:03d}").update(installed_on=ago(today, days + 3))
            continue
        if i % 7 == 0:
            a = eq.create_asset(tag=f"W-{i:03d}", device_model=dm, department=dept, incoming_inspection=eq.INCOMING_WAITING, added_on=ago(today, days),
                                today=today)
            Asset.objects.filter(pk=a.pk).update(created_at=_noon(ago(today, days)))
            eq.use_before_inspection(a, UseBeforeInspection.EMERGENCY, today=ago(today, days))
            if i % 2:
                wo = inspections.open_inspection(a)
                assign(wo, technician=tech)
                complete_work_order(wo, inspection_result=InspectionResult.FAILED, results=incoming_results("pass", "fail", "pass", "pass", "pass"),
                                    resolution=MARKER, tag_out=False, today=today)
            continue
        a = _add(f"Q-{i:03d}", dm, dept, today, days, status=AssetStatus.IN_SERVICE if i % 2 else AssetStatus.OUT_OF_SERVICE)
        if i % 3 == 0:
            _inspection(a, today, days, done_ago=0)
        if i % 4 == 0 and a.status == AssetStatus.OUT_OF_SERVICE:
            _in_service(a, today, 0)


def _queries(p) -> int:
    with CaptureQueriesContext(connection) as q:
        s = section.build(p, None)
        for t in s.tables:
            list(t.rows())
    assert s.gaps and s.tables[0].count and s.tables[1].count
    assert {g.kind for g in s.gaps} == {GAP, FINDING, CHECK}
    return len(q.captured_queries)


def test_a_fixed_number_of_queries_whatever_the_size(ctx, today, dept, pump_model, techs):
    p = period(ago(today, 30), today)
    _devices(0, 8, pump_model, dept, today, techs["dana"])
    few = _queries(p)
    _devices(8, 52, pump_model, dept, today, techs["dana"])
    assert _queries(p) == few <= 6


@needs_postgres
def test_the_history_reads_under_row_level_security(tenant, other_tenant):
    """As the runtime role: the devices' history (first status, first day in service) is read inside the facility only."""
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ED")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="C", risk_class=RiskClass.HIGH)
        eq.create_asset(tag="THEIRS-1", device_model=theirs, department=their_dept, status=AssetStatus.IN_SERVICE)
    with tenant_context(tenant):
        today = timezone.localdate()
        dept = Department.objects.create(name="ED")
        dm = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defibrillator", category="Defibrillators",
                                        risk_class=RiskClass.LIFE_SUPPORT)
        a = eq.create_asset(tag="N-1", device_model=dm, department=dept, status=AssetStatus.OUT_OF_SERVICE, today=today)
        eq.set_status(a, AssetStatus.IN_SERVICE, today=today)
    as_app_role()
    with tenant_context(tenant):
        s = section.build(period(ago(today, 5), today), None)
        assert [(r[0], r[4], r[10]) for r in rows_of(s, "new_devices")] == [("N-1", "Out of service", today)]
        assert [(g.kind, g.record) for g in s.gaps] == [(GAP, "N-1")]
