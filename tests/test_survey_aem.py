"""
The survey binder's AEM section (slice 25): the intervals in force with their approval, the evidence the committee saw, and how the
model's devices have done since; the committee's decisions in the period; an interval in force with no recorded approval as a gap;
excluded models (life support, the CMS mark) never counted as on AEM.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from survey_helpers import gaps_of, period, rows_of

from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.models import AemDecision, AemStatus
from apps.reports.survey import GAP
from apps.reports.survey import aem as section
from apps.tenants.context import tenant_context
from apps.workorders.models import PmResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

MARKER = "ZZ-REQUESTER-TEXT-ZZ"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def people(make_user):
    return {"tech": make_user("technician"), "manager": make_user("manager"), "director": make_user("director")}


def _model(name, risk=RiskClass.HIGH, **extra):
    return DeviceModel.objects.create(manufacturer="Acme", model=name, description=name, category="Monitors", risk_class=risk,
                                      oem_pm_interval_months=12, **extra)


def _devices(dm, dept, today, n=3, prefix=None):
    return [Asset.objects.create(tag=f"{prefix or dm.model}-{i}", device_model=dm, department=dept, installed_on=today - timedelta(days=6 * 365),
                                 next_pm_on=today + timedelta(days=60)) for i in range(n)]


def _wo(asset, wtype, opened, **extra):
    return create_work_order(asset=asset, type=wtype, priority=Priority.NORMAL, problem=MARKER, requester=MARKER, opened_on=opened, **extra)


def _figures(s):
    return {f.label: f.value for f in s.figures}


def _day(d):
    return f"{d:%b} {d.day}, {d.year}"


@pytest.fixture
def monitor(ctx, dept, today, people):
    """A model on AEM twice: 18 months approved 280 days ago, replaced by 24 months approved 60 days ago."""
    dm = _model("MX750")
    _devices(dm, dept, today)
    t = lambda days: today - timedelta(days=days)  # noqa: E731
    first = aem.propose(dm, interval_months=18, rationale="Self-test at power-on.", by=people["tech"], today=t(300))
    aem.approve(first, by=people["manager"], decided_on=t(280), note="EMC minutes, item 2", today=t(280))
    second = aem.propose(dm, interval_months=24, rationale="Still no failures.", by=people["tech"], today=t(100))
    aem.approve(second, by=people["manager"], decided_on=t(60), note="EMC minutes, item 4", today=t(60))
    dm.refresh_from_db()
    return dm


def test_an_interval_in_force_with_its_approval_evidence_and_record_since(ctx, today, dept, monitor, people):
    devices = list(Asset.objects.filter(device_model=monitor).order_by("tag"))
    t = lambda days: today - timedelta(days=days)  # noqa: E731
    _wo(devices[0], WoType.REPAIR, t(200))  # under the earlier approval: not since this one
    _wo(devices[0], WoType.REPAIR, t(60))  # on the committee's day: counts
    _wo(devices[1], WoType.REPAIR, t(10))
    cancelled = _wo(devices[2], WoType.REPAIR, t(9))
    WorkOrder.objects.filter(pk=cancelled.pk).update(status=WoStatus.CANCELLED)  # never done: not a failure
    failed = _wo(devices[1], WoType.PM, t(30))
    WorkOrder.objects.filter(pk=failed.pk).update(status=WoStatus.CLOSED, pm_result=PmResult.FAIL, completed_on=t(29))
    passed = _wo(devices[2], WoType.PM, t(30))
    WorkOrder.objects.filter(pk=passed.pk).update(status=WoStatus.CLOSED, pm_result=PmResult.PASS, completed_on=t(29))
    other = _model("Other", aem_interval_months=None)
    _wo(_devices(other, dept, today, n=1)[0], WoType.REPAIR, t(5))  # another model's repair
    s = section.build(period(t(200), today), people["director"])
    decision = AemDecision.objects.get(device_model=monitor, status=AemStatus.APPROVED)
    ev = decision.evidence
    assert rows_of(s, "in_force") == [[
        "Acme", "MX750", "High", 3, 12, 24, t(60), "EMC minutes, item 4", "Technician User", "Manager User", t(100),
        Decimal(str(ev["device_years"])), ev["repairs"], None if ev["repairs_per_device_year"] is None else Decimal(str(ev["repairs_per_device_year"])),
        ev["pm_completed"], ev["pm_on_time"], 2, 1]]
    assert _figures(s)["Models on AEM today"] == 1 and _figures(s)["Devices on AEM today"] == 3 and s.gaps == []
    assert MARKER not in repr([r for tbl in s.tables for r in tbl.rows()])


def test_the_committees_decisions_in_the_period(ctx, today, dept, monitor, people):
    t = lambda days: today - timedelta(days=days)  # noqa: E731
    pump = _model("Pump", RiskClass.MEDIUM)
    _devices(pump, dept, today)
    rejected = aem.propose(pump, interval_months=24, rationale="Few repairs.", by=people["tech"], today=t(150))
    aem.reject(rejected, by=people["manager"], decided_on=t(140), note="EMC minutes, item 7: more history", today=t(140))
    withdrawn = aem.propose(pump, interval_months=18, rationale="Few repairs.", by=people["tech"], today=t(30))
    aem.withdraw(withdrawn, by=people["tech"], today=t(20))
    s = section.build(period(t(200), today), people["director"])
    rows = rows_of(s, "decisions")
    replaced = f"Replaced by the 24-month interval approved on {_day(t(60))}."
    assert [r[:2] + [r[3], r[4]] + r[7:] for r in rows] == [
        [t(140), "Rejected", "Pump", 24, "EMC minutes, item 7: more history", "", "Manager User", "Rejected"],
        [t(60), "Approved", "MX750", 24, "EMC minutes, item 4", "", "Manager User", "Approved"],
        [t(60), "Ended", "MX750", 18, "", replaced, "Manager User", "Ended"],
        [t(20), "Withdrawn", "Pump", 18, "", "Withdrawn by the proposer.", "Technician User", "Withdrawn"],
    ]  # the 18-month approval 280 days ago is before the period
    assert rows[1][5:7] == [12, t(100)]  # OEM interval and proposed on
    assert _figures(s)["AEM decisions in the period"] == 4
    wider = rows_of(section.build(period(t(300), today), people["director"]), "decisions")
    assert [r[:2] + [r[3], r[4]] for r in wider][0] == [t(280), "Approved", "MX750", 18]


def test_an_interval_with_no_recorded_approval_is_a_gap_unless_the_model_is_excluded(ctx, today, dept, pump_model, vent_model, people):
    vent_model.aem_interval_months = 12  # on file for life support: never applies
    vent_model.save()
    imaging = _model("C-arm", oem_schedule_required=True, aem_interval_months=24)  # the CMS mark: never applies
    for dm, prefix in ((pump_model, "P"), (vent_model, "V"), (imaging, "C")):
        _devices(dm, dept, today, n=2, prefix=prefix)
    s = section.build(period(today - timedelta(days=30), today), people["director"])
    assert [(g.kind, g.record, g.url) for g in gaps_of(s)] == [(GAP, "BD Alaris 8015 PCU", f"/pm/models/{pump_model.pk}/?tab=aem")]
    assert gaps_of(s)[0].text == ("BD Alaris 8015 PCU runs on a 18-month AEM interval with no recorded approval: ratify it from the "
                                  "model's AEM tab.")
    assert rows_of(s, "in_force") == [["BD", "Alaris 8015 PCU", "High", 2, 12, 18, None, "No recorded approval", "", ""] + [None] * 8]
    figures = _figures(s)
    assert figures["Models on AEM today"] == 1 and figures["Devices on AEM today"] == 2
    assert figures["Life-support models"] == 1 and figures["Imaging, radiologic, and laser models"] == 1 and figures["Excluded models on AEM"] == 0
    aem.end(pump_model, by=people["manager"], reason="Back to the OEM interval.", today=today)
    after = section.build(period(today - timedelta(days=30), today), people["director"])
    assert after.gaps == [] and rows_of(after, "in_force") == [] and _figures(after)["Models on AEM today"] == 0


def test_another_facilitys_aem_is_never_read(ctx, today, dept, monitor, people, other_tenant):
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="C", risk_class=RiskClass.MEDIUM,
                                            oem_pm_interval_months=12, aem_interval_months=36)
        a = Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=their_dept)
        AemDecision.objects.create(device_model=theirs, interval_months=36, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                                   proposed_on=today - timedelta(days=20), decided_on=today - timedelta(days=10), decision_note="Theirs")
        create_work_order(asset=a, type=WoType.REPAIR, priority=Priority.NORMAL, problem="x", opened_on=today)
    s = section.build(period(today - timedelta(days=30), today), people["director"])
    assert [r[1] for r in rows_of(s, "in_force")] == ["MX750"] and rows_of(s, "in_force")[0][-2:] == [0, 0]
    assert [r[3] for r in rows_of(s, "decisions")] == []  # their approval is in the period; MX750's was 60 days ago
    assert _figures(s)["Models on AEM today"] == 1


def _aem_models(first, n, dept, today, users):
    for i in range(first, first + n):
        dm = _model(f"Q{i:03d}", aem_interval_months=24)
        a = _devices(dm, dept, today, n=1)[0]
        _wo(a, WoType.REPAIR, today - timedelta(days=3))
        if i % 3:  # approved; the rest are on file without a recorded approval (gaps)
            AemDecision.objects.create(device_model=dm, interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                                       evidence={"as_of": (today - timedelta(days=40)).isoformat(), "device_years": 3.5, "repairs": 2,
                                                 "repairs_per_device_year": 0.57, "pm_completed": 6, "pm_on_time": 6},
                                       proposed_by=users["tech"], proposed_on=today - timedelta(days=40), decided_by=users["manager"],
                                       decided_on=today - timedelta(days=20), decision_note=f"Item {i}")
        AemDecision.objects.create(device_model=dm, interval_months=36, oem_interval_months=12, status=AemStatus.REJECTED, rationale="r",
                                   proposed_by=users["tech"], proposed_on=today - timedelta(days=50), decided_by=users["manager"],
                                   decided_on=today - timedelta(days=45), decision_note="No")


def _queries(p, user) -> int:
    with CaptureQueriesContext(connection) as q:
        s = section.build(p, user)
        for t in s.tables:
            list(t.rows())
    assert s.gaps and all(t.count for t in s.tables)
    return len(q.captured_queries)


def test_a_fixed_number_of_queries_whatever_the_size(ctx, today, dept, people):
    p = period(today - timedelta(days=100), today)
    _aem_models(0, 5, dept, today, people)
    few = _queries(p, people["director"])
    _aem_models(5, 45, dept, today, people)
    assert _queries(p, people["director"]) == few <= 5
