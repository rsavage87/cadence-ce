"""
Slice 25 review fixes on the survey binder: a gap links only to what its reader may open (page, print, gaps CSV, API); a PM cancelled
while its device was retired on its due date stays excused whatever happened to the device later; a PM due on the last day of a past
period and never done is counted (pm_due_queryset read on today: the binder, the Overview, and the monthly series agree); a failed PM
recorded on a repair already open names that repair; a recall match reviewed before and open again is a check, never a late first
review.
"""
from datetime import date, timedelta

import pytest
from survey_helpers import gaps_of, period, rows_of
from test_survey_maintenance import build, device, models, pm, today  # noqa: F401  (fixtures and helpers)
from test_survey_recalls import P, S, match

from apps.credentials.models import Technician
from apps.equipment import services as eq
from apps.equipment.models import AssetStatus
from apps.facility.models import FacilitySettings
from apps.pm.dates import month_bounds
from apps.pm.services import missed_pms, pm_due_queryset, pm_on_time_rate, pm_on_time_series
from apps.recalls import services as recall_services
from apps.reports.services import overview_kpis
from apps.reports.survey import CHECK, GAP, maintenance, recalls
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PmResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

OLD_TEXTS = {"policy_life_support": "OEM interval, complete within due month, no grace", "policy_medium_low": "AEM allowed, 30-day grace after due month"}


@pytest.fixture
def old_policy(ctx):
    """A facility that saved Settings before slice 25 keeps the old default texts, which mention the due month: two Program checks
    linking to Settings."""
    FacilitySettings.objects.update_or_create(tenant=ctx, defaults=OLD_TEXTS)


@pytest.mark.parametrize("role, linked", [("technician", False), ("analyst", False), ("director", True)])
def test_a_gap_links_only_to_what_its_reader_may_open(client, make_user, old_policy, role, linked):
    client.force_login(make_user(role))
    page = client.get("/reports/survey/").content.decode()
    assert ('href="/settings/"' in page) is linked
    assert ("/settings/?facility=" in client.get("/print/survey/").content.decode()) is linked
    assert ("/settings/" in client.get("/reports/survey/gaps.csv").getvalue().decode()) is linked
    gaps = client.get("/api/v1/survey/program/").json()["gaps"]
    assert gaps and all(g["kind"] == CHECK and g["record"] == "Settings" for g in gaps)
    assert all(bool(g["url"]) is linked for g in gaps)


def test_a_pm_due_while_the_device_was_retired_stays_excused_after_a_second_retirement(ctx, today, dept, models):  # noqa: F811
    T = today
    d = device("RR-1", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    excused = pm(d, T - timedelta(days=80))
    eq.set_status(d, AssetStatus.RETIRED, changed_on=T - timedelta(days=90), today=T)  # cancels it; retired on its due date
    eq.set_status(d, AssetStatus.IN_SERVICE, changed_on=T - timedelta(days=60), today=T)
    between = pm(d, T - timedelta(days=30))  # fell due while the device was in use again
    eq.set_status(d, AssetStatus.RETIRED, changed_on=T - timedelta(days=5), today=T)  # cancels `between`
    for wo in (excused, between):
        wo.refresh_from_db()
        assert wo.status == WoStatus.CANCELLED
    counted = set(pm_due_queryset(T - timedelta(days=100), T, T).values_list("number", flat=True))
    assert counted == {between.number}
    assert set(missed_pms(T).values_list("number", flat=True)) == {between.number}
    assert [g.record for g in gaps_of(build(T), GAP) if g.record.startswith("WO-")] == [between.number]
    eq.set_status(d, AssetStatus.IN_SERVICE, changed_on=T - timedelta(days=1), today=T)  # reinstated today: what came after does not matter
    assert set(pm_due_queryset(T - timedelta(days=100), T, T).values_list("number", flat=True)) == {between.number}


def test_a_pm_due_on_the_last_day_of_a_past_period_and_never_done_is_counted(ctx, today, dept, models):  # noqa: F811
    T = today
    start, end = month_bounds(*(lambda d: (d.year, d.month))(T.replace(day=1) - timedelta(days=40)))
    d = device("LD-1", models["ls"], dept, T - timedelta(days=400), next_pm_on=T + timedelta(days=100))
    never = pm(d, end)
    s = maintenance.build(period(start, end, T), None)
    assert [r[0] for r in rows_of(s, "not_on_time")] == [never.number]
    assert never.number in {g.record for g in gaps_of(s, GAP)}
    kpi = pm_on_time_rate(start, end, T)
    assert kpi["due"] == 1 and kpi["on_time"] == 0
    assert overview_kpis(start.year, start.month, today=T)["pm_on_time"]["due"] == 1
    point = next(p for p in pm_on_time_series(T.year, T.month, months=6, today=T) if (p["year"], p["month"]) == (start.year, start.month))
    assert (point["due"], point["on_time"]) == (1, 0)
    current = pm(d, T)  # due today in the current period: not counted until done
    assert not pm_due_queryset(T.replace(day=1), T, T).filter(pk=current.pk).exists()


def test_a_failure_recorded_on_a_repair_already_open_names_that_repair(ctx, today, dept, models, make_user):  # noqa: F811
    T, kim, dana = today, make_user("director"), Technician.objects.create(name="Dana Whitfield")
    d = device("FR-1", models["ls"], dept, T - timedelta(days=400), next_pm_on=T + timedelta(days=100))
    first = create_work_order(asset=d, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=T - timedelta(days=3), due_on=T + timedelta(days=3),
                              assigned_to=dana)
    repair = complete_work_order(first, pm_result=PmResult.FAIL, resolution="Leak current over the limit", by=kim, today=T).repair
    second = create_work_order(asset=d, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=T - timedelta(days=2), due_on=T + timedelta(days=4),
                               assigned_to=dana)
    done = complete_work_order(second, pm_result=PmResult.FAIL, resolution="Alarm silent", open_repair=False, by=kim, today=T)
    assert done.repair.pk == repair.pk and WorkOrder.objects.get(pk=repair.pk).follow_up_of_id == first.pk
    rows = {r[0]: r for r in rows_of(build(T), "failed")}
    assert rows[first.number][3] == repair.number and rows[second.number][3] == repair.number
    assert rows[second.number][4] == WoStatus(repair.status).label


def test_a_recall_reviewed_before_and_open_again_is_a_check_not_a_late_first_review(ctx, pump_model):
    reopened = match("Z-RE", pump_model, date(2026, 1, 10))
    recall_services.set_status(reopened, S.NOT_AFFECTED, note="Our units are the 2019 revision, not affected", today=date(2026, 1, 12))
    recall_services.set_status(reopened, S.UNDER_REVIEW, today=date(2026, 10, 6))
    waiting = match("Z-WAIT", pump_model, date(2026, 6, 1))
    s = recalls.build(P, None)
    assert [g.record for g in gaps_of(s, GAP)] == ["FDA Z-WAIT"]
    checks = gaps_of(s, CHECK)
    assert [g.record for g in checks] == ["FDA Z-RE"] and "reviewed before" in checks[0].text and "waited" not in checks[0].text
    assert waiting.status == S.NEEDS_ACTION
