"""
The survey binder's "Recalls and safety alerts" section (slice 25, part 3): the matches received in the period and every one still open,
dated by when they reached the facility (never before the notice was published), with the Recall response log's words, and a gap for a
match left waiting for its review more than REVIEW_DAYS days.
"""
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.recalls.models import Alert, AlertMatch
from apps.reports.survey import GAP, recalls
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoStatus, WoType

TODAY = date(2026, 10, 7)
P = period(date(2026, 1, 1), TODAY)
S = AlertMatch.Status


def at(day: date, hour: int = 12, minute: int = 0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


def match(external_id, model, received, published=None, status=S.NEEDS_ACTION, classification="Class II", **extra):
    """A match created on `received` (the facility's day) for a new notice published on `published` (the same day by default)."""
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id=external_id, classification=classification,
                                 manufacturer=model.manufacturer, product=model.model, title=f"Notice {external_id}",
                                 published_on=published or received)
    m = AlertMatch.objects.create(alert=alert, device_model=model, status=status, **extra)
    AlertMatch.objects.filter(pk=m.pk).update(created_at=at(received))
    m.refresh_from_db()
    return m


def labels(section) -> list[str]:
    return [r[2] for r in rows_of(section, "matches")]


def test_received_is_the_facilitys_day_never_before_the_notice(tenant, pump_model):
    later = match("Z-1", pump_model, date(2026, 3, 1), published=date(2026, 2, 20))
    early = match("Z-2", pump_model, date(2026, 2, 10), published=date(2026, 3, 5))  # matched before its published date: never
    assert recalls.received_on(later) == date(2026, 3, 1) and recalls.received_on(early) == date(2026, 3, 5)
    AlertMatch.objects.filter(pk=later.pk).update(created_at=datetime(2026, 3, 10, 8, 30, tzinfo=dt_timezone.utc))
    later.refresh_from_db()
    tenant.timezone = "Pacific/Honolulu"
    with tenant_context(tenant):
        assert recalls.received_on(later) == date(2026, 3, 9)  # 22:30 on Mar 9 in Honolulu
    tenant.timezone = "Asia/Tokyo"
    with tenant_context(tenant):
        assert recalls.received_on(later) == date(2026, 3, 10)


def test_rows_received_in_the_period_and_every_open_one_class_one_first(ctx, pump_model, vent_model):
    match("Z-OLD-CLOSED", pump_model, date(2025, 11, 1), status=S.CLOSED, closed_on=date(2025, 11, 20))  # before the period, done
    match("Z-OLD-OPEN", pump_model, date(2025, 10, 1), status=S.UNDER_REVIEW)  # before the period, still open
    match("Z-NOT", pump_model, date(2026, 2, 1), status=S.NOT_AFFECTED, closed_on=date(2026, 2, 3))
    match("Z-CLOSED", pump_model, date(2026, 6, 1), status=S.CLOSED, closed_on=date(2026, 6, 11), disposition_note="Firmware updated")
    match("Z-CLASS-I", vent_model, date(2026, 4, 1), classification="Class I", status=S.IN_PROGRESS)
    match("Z-CLASS-I-DONE", vent_model, date(2026, 9, 1), classification="class i", status=S.CLOSED, closed_on=date(2026, 9, 2))
    match("Z-NEW", pump_model, date(2026, 9, 30))
    match("Z-LATE-PUB", pump_model, date(2025, 12, 20), published=date(2026, 1, 5), status=S.CLOSED, closed_on=date(2026, 1, 9))
    s = recalls.build(P, None)
    # Open Class I first, then newest received (a closed Class I is not pulled up); the closed one from before the period is left out.
    assert labels(s) == ["FDA Z-CLASS-I", "FDA Z-NEW", "FDA Z-CLASS-I-DONE", "FDA Z-CLOSED", "FDA Z-NOT", "FDA Z-LATE-PUB", "FDA Z-OLD-OPEN"]
    assert s.table("matches").count == 7
    assert s.key == "recalls" and s.title == "Recalls and safety alerts"
    assert s.covers == "Jan 1, 2026 to Oct 7, 2026, and every match still open as of today, Oct 7, 2026"


def test_columns_response_and_days(ctx, dept, pump_model, vent_model, pump):
    vent1 = Asset.objects.create(tag="CE-20001", device_model=vent_model, department=dept)
    vent2 = Asset.objects.create(tag="CE-20002", device_model=vent_model, department=dept)
    Asset.objects.create(tag="CE-20003", device_model=vent_model, department=dept, status=AssetStatus.RETIRED)
    closed = match("Z-CLOSED", pump_model, date(2026, 6, 1), published=date(2026, 5, 28), status=S.CLOSED, closed_on=date(2026, 6, 11),
                   disposition_note="Firmware updated on all pumps")
    progressing = match("Z-PROG", vent_model, date(2026, 9, 1), classification="Class I", status=S.IN_PROGRESS)
    for asset, finished in ((vent1, True), (vent1, True), (vent2, False)):
        wo = WorkOrder.objects.create(asset=asset, type=WoType.RECALL, problem="Recall", opened_on=date(2026, 9, 2), due_on=date(2026, 9, 16),
                                      alert=progressing.alert)
        if finished:
            WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=date(2026, 9, 5))
    waiting = match("Z-WAIT", pump_model, date(2026, 9, 27), classification="")
    rows = {r[2]: r for r in rows_of(recalls.build(P, None), "matches")}
    assert recalls.COLUMNS == ["Received", "Published", "Alert", "Source", "Class", "Manufacturer", "Model", "Devices affected", "Status",
                               "Response", "Closed on", "Days to close", "Days open"]
    assert rows["FDA Z-CLOSED"] == [date(2026, 6, 1), date(2026, 5, 28), "FDA Z-CLOSED", "FDA", "Class II", "BD", "Alaris 8015 PCU", 1, "Closed",
                                    "Firmware updated on all pumps", date(2026, 6, 11), 10, None]
    assert rows["FDA Z-PROG"][7:] == [2, "Action in progress", "1 of 2 devices completed", None, None, 36]
    assert rows["FDA Z-WAIT"][4] == "" and rows["FDA Z-WAIT"][8:] == ["Needs action", "Open", None, None, 10]
    assert closed.pk and waiting.pk


def test_a_match_waiting_more_than_fourteen_days_for_review_is_a_gap(ctx, pump_model, vent_model):
    late = match("Z-LATE", vent_model, TODAY - timedelta(days=15), classification="Class I")
    match("Z-FRESH", pump_model, TODAY - timedelta(days=14))  # 14 days: not yet
    reviewing = match("Z-REVIEW", pump_model, TODAY - timedelta(days=40), published=TODAY - timedelta(days=20), status=S.UNDER_REVIEW)
    match("Z-PROG", pump_model, TODAY - timedelta(days=30), status=S.IN_PROGRESS)  # acted on: no gap
    match("Z-DONE", pump_model, TODAY - timedelta(days=60), status=S.CLOSED, closed_on=TODAY - timedelta(days=50))
    s = recalls.build(P, None)
    gaps = gaps_of(s)
    assert [g.kind for g in gaps] == [GAP, GAP]
    by_record = {g.record: g for g in gaps}
    assert set(by_record) == {"FDA Z-LATE", "FDA Z-REVIEW"}
    g = by_record["FDA Z-LATE"]
    assert g.text == ("Class I FDA Z-LATE on Hamilton Medical Hamilton-G5 has waited 15 days for a review (received Sep 22, 2026, "
                      "needs action)")
    assert g.url == f"/recalls/?match={late.pk}"
    assert "has waited 20 days" in by_record["FDA Z-REVIEW"].text  # counted from its published date, the later one
    assert by_record["FDA Z-REVIEW"].url == f"/recalls/?match={reviewing.pk}"
    assert any(f"more than {recalls.REVIEW_DAYS} days" in n for n in s.notes) and recalls.REVIEW_DAYS == 14


def test_figures(ctx, pump_model, vent_model):
    match("Z-A", pump_model, date(2026, 3, 1), status=S.CLOSED, closed_on=date(2026, 3, 5))  # 4 days
    match("Z-B", pump_model, date(2026, 4, 1), status=S.NOT_AFFECTED, closed_on=date(2026, 4, 11))  # 10 days
    match("Z-C", pump_model, date(2026, 5, 1), status=S.CLOSED, closed_on=date(2026, 5, 7))  # 6 days
    match("Z-D", vent_model, date(2026, 9, 20), classification="Class I")
    match("Z-E", vent_model, date(2025, 6, 1), classification="Class I", status=S.IN_PROGRESS)  # open, received before the period
    match("Z-F", pump_model, date(2026, 8, 1), status=S.UNDER_REVIEW)
    figures = {f.label: f.value for f in recalls.build(P, None).figures}
    assert figures == {"Received in the period": 5, "Closed": 3, "Median days to close": 6, "Open now": 3, "Open Class I": 2}


def test_an_empty_facility(ctx):
    s = recalls.build(P, None)
    assert rows_of(s, "matches") == [] and gaps_of(s) == []
    assert {f.label: f.value for f in s.figures}["Median days to close"] == "None closed"


def test_another_facilitys_matches_never_appear(ctx, pump_model, other_tenant):
    mine = match("Z-SHARED", pump_model, date(2026, 5, 1))
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        dept = Department.objects.create(name="ICU")
        Asset.objects.create(tag="THEIRS-1", device_model=dm, department=dept)
        AlertMatch.objects.create(alert=mine.alert, device_model=dm, status=S.UNDER_REVIEW)
        match("Z-THEIRS", dm, date(2026, 5, 2))
    s = recalls.build(P, None)
    assert labels(s) == ["FDA Z-SHARED"] and rows_of(s, "matches")[0][7] == 0  # none of their devices
    assert [g.record for g in gaps_of(s)] == ["FDA Z-SHARED"]


def _populate(models, start, n):
    for i in range(start, start + n):
        status = [S.NEEDS_ACTION, S.UNDER_REVIEW, S.IN_PROGRESS, S.CLOSED, S.NOT_AFFECTED][i % 5]
        closed_on = date(2026, 9, 1) if status in (S.CLOSED, S.NOT_AFFECTED) else None
        m = match(f"Z-{i:04d}", models[i % 2], date(2026, 1, 1) + timedelta(days=i * 4), status=status, closed_on=closed_on,
                  classification="Class I" if i % 3 == 0 else "Class II")
        if status == S.IN_PROGRESS:
            asset = m.device_model.assets.first()
            WorkOrder.objects.create(asset=asset, type=WoType.RECALL, problem="Recall", opened_on=date(2026, 9, 2), due_on=date(2026, 9, 16),
                                     alert=m.alert, status=WoStatus.COMPLETED, completed_on=date(2026, 9, 3))


def _queries() -> int:
    with CaptureQueriesContext(connection) as q:
        s = recalls.build(P, None)
        for t in s.tables:
            list(t.rows())
    return len(q.captured_queries)


def test_a_fixed_number_of_queries(ctx, vent, pump):
    models = [vent.device_model, pump.device_model]
    _populate(models, 0, 5)
    small = _queries()
    _populate(models, 5, 45)
    assert len(rows_of(recalls.build(P, None), "matches")) == 50
    assert _queries() == small <= 3  # the matches, the in-progress counts, and which waiting ones were reviewed before (review fix)


@needs_postgres
def test_the_section_reads_as_the_runtime_role(tenant, other_tenant, pump_model):
    mine = match("Z-MINE", pump_model, date(2026, 5, 1))
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        match("Z-THEIRS", dm, date(2026, 5, 2))
    as_app_role()
    with tenant_context(tenant):
        s = recalls.build(P, None)
    assert labels(s) == ["FDA Z-MINE"] and [g.url for g in gaps_of(s)] == [f"/recalls/?match={mine.pk}"]


@pytest.mark.parametrize("text, one", [("Class I", True), ("class i", True), (" Class I (FDA) ", True), ("Class II", False), ("", False)])
def test_class_one(text, one):
    assert recalls.is_class_one(text) is one
