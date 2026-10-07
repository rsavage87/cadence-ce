"""
The survey binder's inventory section (slice 25): the devices in use by category and risk class, every active device with how it is
maintained, risk class changes in the period from the models' history, the devices marked missing, and its gaps.
"""
from datetime import datetime, time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, inventory
from apps.tenants.context import tenant_context

MARKER = "ZZ-REQUESTER-TEXT-ZZ"


def _noon(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def year(today):
    return period(today - timedelta(days=200), today)


def _model(name, category, risk=RiskClass.MEDIUM, **extra):
    return DeviceModel.objects.create(manufacturer="Acme", model=name, description=name, category=category, risk_class=risk, **extra)


def _device(tag, dm, dept, **extra):
    extra.setdefault("next_pm_on", timezone.localdate() + timedelta(days=30))
    return Asset.objects.create(tag=tag, device_model=dm, department=dept, **extra)


def _figures(section):
    return {f.label: f.value for f in section.figures}


def test_the_inventory_by_category_and_class_and_every_device(ctx, today, year, dept, vent_model, pump_model):
    vent_model.aem_interval_months = 12  # on file for a life-support model: never applies
    vent_model.save()
    bed = _model("Bed 1", "Beds", RiskClass.LOW)
    v = _device("V-1", vent_model, dept, room="12", last_pm_on=today - timedelta(days=100), notes=MARKER)
    _device("V-2", vent_model, dept, status=AssetStatus.IN_REPAIR)
    _device("P-1", pump_model, dept)
    eq.create_asset(tag="B-1", device_model=bed, department=dept, added_as=AddedAs.EXISTING, today=today)
    _device("B-2", bed, dept, status=AssetStatus.RETIRED, next_pm_on=None)  # retired: out of the inventory
    s = inventory.build(year, None)
    figures = _figures(s)
    assert figures["Active devices"] == 4 and figures["Life-support devices"] == 2 and figures["High-risk devices"] == 1
    assert figures["Medium-risk devices"] == 0 and figures["Low-risk devices"] == 1 and figures["Devices marked missing"] == 0
    assert figures["Models not risk-scored"] == 3  # every model in use, scored by hand
    assert rows_of(s, "by_category") == [["Beds", 0, 0, 0, 1, 1], ["Infusion pumps", 0, 1, 0, 0, 1], ["Ventilators", 2, 0, 0, 0, 2]]
    devices = s.table("devices")
    assert devices.printed is False and devices.count == 4 and devices.links == {0: DEVICE}
    rows = {r[0]: r for r in rows_of(s, "devices")}
    assert list(rows) == ["B-1", "P-1", "V-1", "V-2"]
    assert rows["V-1"] == ["V-1", "Hamilton Medical", "Hamilton-G5", "ICU ventilator", "Ventilators", "Life support", "ICU", "12", "In service",
                           "OEM", 6, today - timedelta(days=100), today + timedelta(days=30), "Not recorded"]
    assert rows["P-1"][9:11] == ["AEM", 18]  # the pump model's approved interval applies
    assert rows["B-1"][-1] == "Already in use here" and rows["V-2"][8] == "In repair"
    assert MARKER not in repr([r for t in s.tables for r in t.rows()]) + repr(s.gaps) and v.notes == MARKER
    assert s.covers.startswith("Devices as of today") and s.gaps == []


def test_a_device_with_no_next_pm_is_a_gap(ctx, year, dept, vent_model):
    _device("V-1", vent_model, dept, next_pm_on=None)
    _device("V-2", vent_model, dept, status=AssetStatus.RETIRED, next_pm_on=None)  # retired: off the schedule on purpose
    gaps = gaps_of(inventory.build(year, None))
    assert [(g.kind, g.record, g.url) for g in gaps] == [(GAP, "V-1", "/equipment/V-1/")]
    assert "V-1 (Hamilton Medical Hamilton-G5) is in use with no next PM date" in gaps[0].text


def test_missing_devices_with_the_day_they_went_missing(ctx, today, year, dept, vent_model, pump_model):
    bed = _model("Bed 1", "Beds", RiskClass.LOW)
    v = _device("V-1", vent_model, dept)
    b = _device("B-1", bed, dept)
    p = _device("P-1", pump_model, dept)
    Asset.history.filter(history_type="+").update(history_date=_noon(today - timedelta(days=60)))  # added before they went missing
    eq.set_status(v, AssetStatus.MISSING, changed_on=today - timedelta(days=9), today=today)
    eq.set_status(b, AssetStatus.MISSING, changed_on=today - timedelta(days=3), today=today)
    eq.set_status(p, AssetStatus.MISSING, changed_on=today - timedelta(days=30), today=today)  # missing, found, missing again
    eq.set_status(p, AssetStatus.IN_SERVICE, changed_on=today - timedelta(days=20), today=today)
    eq.set_status(p, AssetStatus.MISSING, changed_on=today - timedelta(days=5), today=today)
    s = inventory.build(year, None)
    assert _figures(s)["Devices marked missing"] == 3 and _figures(s)["Active devices"] == 3
    assert rows_of(s, "missing") == [["B-1", "Acme Bed 1", "Low", today - timedelta(days=3)],
                                     ["P-1", "BD Alaris 8015 PCU", "High", today - timedelta(days=5)],
                                     ["V-1", "Hamilton Medical Hamilton-G5", "Life support", today - timedelta(days=9)]]
    findings = gaps_of(s, FINDING)
    assert [(g.record, g.url) for g in findings] == [("P-1", "/equipment/P-1/"), ("V-1", "/equipment/V-1/")]  # not the low-risk bed
    since = today - timedelta(days=9)
    assert findings[1].text == f"V-1 (Hamilton Medical Hamilton-G5, life support) has been missing since {since:%b} {since.day}, {since.year}."
    Asset.objects.filter(pk=b.pk).update(status=AssetStatus.MISSING)  # no history row says when (written without the services)
    Asset.history.filter(id=b.pk, status=AssetStatus.MISSING).delete()
    assert rows_of(inventory.build(year, None), "missing")[0] == ["B-1", "Acme Bed 1", "Low", None]


def test_a_scored_models_overdue_yearly_review_is_a_check(ctx, today, year, dept):
    due = _model("Due", "Pumps")
    fresh = _model("Fresh", "Pumps")
    idle = _model("Idle", "Pumps")  # scored, overdue, but no device in use
    _model("Hand", "Pumps")  # never scored: a figure, not a check
    for dm in (due, fresh, idle):
        eq.set_risk_score(dm, function=5, physical=2, maintenance=2, incidents=0, today=today)
    DeviceModel.objects.filter(pk__in=[due.pk, idle.pk]).update(risk_reviewed_on=today - timedelta(days=400))
    hand = DeviceModel.objects.get(model="Hand")
    for i, dm in enumerate((due, fresh, idle, hand)):
        _device(f"D-{i}", dm, dept, status=AssetStatus.RETIRED if dm == idle else AssetStatus.IN_SERVICE)
    s = inventory.build(year, None)
    checks = gaps_of(s, CHECK)
    assert [(g.record, g.url) for g in checks] == [("Acme Due", f"/pm/models/{due.pk}/")]
    assert "the facility's yearly review" in checks[0].text and "last reviewed" in checks[0].text
    assert _figures(s)["Models not risk-scored"] == 1


def test_risk_class_changes_in_the_period_from_the_models_history(ctx, today, year, dept, make_user):
    director = make_user("director")
    dm = _model("Pump", "Pumps", RiskClass.LOW)
    other = _model("Monitor", "Monitors", RiskClass.LOW)
    eq.set_risk_score(dm, function=6, physical=3, maintenance=2, incidents=1, by=director, today=today)  # 12: high
    eq.update_device_model(dm, description="Renamed pump", by=director)  # not a class change
    eq.set_risk_score(other, function=5, physical=2, maintenance=2, incidents=0, today=today)  # 9: medium
    DeviceModel.history.filter(id=other.pk, history_type="+").update(history_date=_noon(year.start - timedelta(days=2)))
    DeviceModel.history.filter(id=other.pk, history_type="~").update(history_date=_noon(year.start - timedelta(days=1)))  # before the period
    s = inventory.build(year, None)
    assert rows_of(s, "risk_changes") == [["Acme Pump", "Low", "High", today, "Director User"]]
    assert s.table("risk_changes").count is None  # read only when its rows are
    wider = period(year.start - timedelta(days=1), today)
    assert [r[:3] + [r[4]] for r in rows_of(inventory.build(wider, None), "risk_changes")] == [
        ["Acme Monitor", "Low", "Medium", "Cadence"], ["Acme Pump", "Low", "High", "Director User"]]


def test_another_facilitys_devices_and_history_are_never_read(ctx, today, year, dept, vent_model, other_tenant):
    _device("V-1", vent_model, dept)
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="Ventilators", risk_class=RiskClass.LOW)
        eq.set_risk_score(theirs, function=10, physical=5, maintenance=5, incidents=2)
        a = Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=their_dept)
        eq.set_status(a, AssetStatus.MISSING)
        Asset.objects.create(tag="THEIRS-2", device_model=theirs, department=their_dept, next_pm_on=None)
    s = inventory.build(year, None)
    assert _figures(s)["Active devices"] == 1 and _figures(s)["Devices marked missing"] == 0 and s.gaps == []
    assert [r[0] for r in rows_of(s, "devices")] == ["V-1"] and rows_of(s, "risk_changes") == [] and rows_of(s, "missing") == []


def _models(today):
    models = [_model(f"M{i}", f"Cat {i % 3}", list(RiskClass)[i % 4], aem_interval_months=24 if i % 2 else None) for i in range(4)]
    for dm in models[:2]:
        eq.set_risk_score(dm, function=5, physical=2, maintenance=2, incidents=0, today=today - timedelta(days=500))
    return models


def _devices(models, first, n, dept, today):
    for i in range(first, first + n):
        a = _device(f"Q-{i:03d}", models[i % 4], dept, next_pm_on=None if i % 7 == 0 else today + timedelta(days=i))
        if i % 5 == 0:
            eq.set_status(a, AssetStatus.MISSING, today=today)


def _queries(year) -> int:
    with CaptureQueriesContext(connection) as q:
        s = inventory.build(year, None)
        for t in s.tables:
            list(t.rows())
    assert s.gaps and all(t.count != 0 for t in s.tables if t.key != "risk_changes")
    return len(q.captured_queries)


def test_a_fixed_number_of_queries_whatever_the_size(ctx, today, year, dept):
    models = _models(today)
    _devices(models, 0, 5, dept, today)
    few = _queries(year)
    _devices(models, 5, 45, dept, today)
    assert _queries(year) == few <= 9


@needs_postgres
def test_the_history_reads_under_row_level_security(tenant, other_tenant, make_user):
    """As the runtime role: the models' and devices' history is read inside the facility (its rows only, through core.history._rows)."""
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="X", model="Theirs", description="X", category="C", risk_class=RiskClass.LOW)
        eq.set_risk_score(theirs, function=10, physical=5, maintenance=5, incidents=2)
        eq.set_status(Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=their_dept), AssetStatus.MISSING)
    with tenant_context(tenant):
        today = timezone.localdate()
        dept = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="Acme", model="Pump", description="Pump", category="Pumps", risk_class=RiskClass.LOW)
        eq.set_risk_score(dm, function=6, physical=3, maintenance=2, incidents=1)
        eq.set_status(Asset.objects.create(tag="P-1", device_model=dm, department=dept, next_pm_on=today), AssetStatus.MISSING)
    as_app_role()
    with tenant_context(tenant):
        s = inventory.build(period(today - timedelta(days=30), today), None)
        assert rows_of(s, "risk_changes") == [["Acme Pump", "Low", "High", today, "Cadence"]]
        assert rows_of(s, "missing") == [["P-1", "Acme Pump", "High", today]] and [r[0] for r in rows_of(s, "devices")] == ["P-1"]
