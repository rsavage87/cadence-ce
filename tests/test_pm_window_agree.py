"""
Slice 27, after the merge: one definition everywhere. For the default window, each kind for both groups, and a mixed pair, set in
Settings: the on-time rate, the series point, and the Overview agree month by month; the custom report's PM on time column says True for
exactly the PMs counted on time and False for exactly those counted late; the binder's "PMs not on time" are exactly missed_pms in its
period; report_compliance's "Overdue now" is assets_past_window plus the missing devices; and a PM completed today asks why it was late
(completes_late) exactly when completing it leaves it among missed_pms.
"""
from datetime import date, timedelta

import pytest
from survey_helpers import period, rows_of

from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import PmWindow as K
from apps.pm import windows as W
from apps.pm.dates import month_bounds
from apps.pm.services import assets_past_window, missed_pms, pm_due_queryset, pm_on_time_rate, pm_on_time_series
from apps.reports import custom
from apps.reports.fleet import report_compliance
from apps.reports.services import overview_kpis
from apps.reports.survey import maintenance
from apps.workorders.completion import completes_late
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

TODAY = date(2026, 10, 20)
WINDOWS = {
    "default": {},
    "days14": {"pm_window_high": K.DAYS_AFTER, "pm_window_high_days": 14, "pm_window_other": K.DAYS_AFTER, "pm_window_other_days": 14},
    "due_month": {"pm_window_high": K.DUE_MONTH, "pm_window_other": K.DUE_MONTH},
    "next_month": {"pm_window_high": K.NEXT_MONTH, "pm_window_other": K.NEXT_MONTH},
    "mixed": {"pm_window_high": K.DUE_MONTH, "pm_window_other": K.DAYS_AFTER, "pm_window_other_days": 10},
}


@pytest.fixture
def fleet(ctx, vent_model, dept):
    """PMs due over four months on a life-support and a medium device: done early, on the day, a few days late, the next month, two
    months later, still open, and cancelled on a device in use; devices with next PMs on each side of every window; one missing."""
    medium = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Monitor", category="Monitors",
                                        risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    pms = []
    for i, model in enumerate((vent_model, medium)):
        for j, (due, after, status) in enumerate([
                (date(2026, 7, 10), -2, WoStatus.COMPLETED), (date(2026, 7, 31), 0, WoStatus.COMPLETED), (date(2026, 8, 3), 5, WoStatus.COMPLETED),
                (date(2026, 8, 20), 20, WoStatus.COMPLETED), (date(2026, 9, 2), 45, WoStatus.COMPLETED), (date(2026, 9, 15), None, WoStatus.OPEN),
                (date(2026, 9, 29), None, WoStatus.CANCELLED), (date(2026, 10, 2), 3, WoStatus.COMPLETED), (date(2026, 10, 9), None, WoStatus.OPEN),
                (date(2026, 10, 19), None, WoStatus.OPEN)]):
            device = Asset.objects.create(tag=f"A-{i}-{j}", device_model=model, department=dept, next_pm_on=due)
            wo = create_work_order(asset=device, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=30),
                                   due_on=due)
            if after is not None:
                WorkOrder.objects.filter(pk=wo.pk).update(status=status, completed_on=due + timedelta(days=after))
            elif status != WoStatus.OPEN:
                WorkOrder.objects.filter(pk=wo.pk).update(status=status)
            pms.append(wo)
    Asset.objects.create(tag="LOST", device_model=medium, department=dept, status=AssetStatus.MISSING, next_pm_on=date(2027, 1, 1))
    return pms


@pytest.mark.parametrize("name", list(WINDOWS))
def test_every_figure_agrees(fleet, name, make_user, monkeypatch):
    from django.utils import timezone

    monkeypatch.setattr(timezone, "localdate", lambda *a, **kw: TODAY if not a else a[0].date())
    if WINDOWS[name]:
        fac.update_settings(by=make_user("director"), **WINDOWS[name])
    w = W.windows()

    for point in pm_on_time_series(TODAY.year, TODAY.month, months=4, today=TODAY, w=w):
        start, end = month_bounds(point["year"], point["month"])
        rate = pm_on_time_rate(start, end, TODAY, w=w)
        assert (point["due"], point["on_time"]) == (rate["due"], rate["on_time"]), (name, point)
        overview = overview_kpis(point["year"], point["month"], today=TODAY)["pm_on_time"]
        assert (overview["due"], overview["on_time"]) == (rate["due"], rate["on_time"]), (name, point)

    counted = pm_due_queryset(date(2026, 1, 1), TODAY, TODAY, w=w)
    on_time = set(counted.filter(w.on_time_q()).values_list("number", flat=True))
    late = set(counted.values_list("number", flat=True)) - on_time
    column = {row[0]: row[1] for row in custom.run(custom.clean_definition("work_orders", ["number", "pm_on_time"]), TODAY)["rows"]}
    assert {n for n, v in column.items() if v is True} == on_time, name
    assert {n for n, v in column.items() if v is False} == late, name

    missed = set(missed_pms(TODAY, w=w).values_list("number", flat=True))
    assert missed == late, name  # counted and not on time is exactly past the window and not on time
    s = maintenance.build(period(date(2026, 7, 1), TODAY), None)
    assert {r[0] for r in rows_of(s, "not_on_time")} == missed, name

    classes = {c["key"]: c for c in report_compliance(TODAY)["classes"]}
    past = {}
    for rc in assets_past_window(TODAY, w=w).values_list("device_model__risk_class", flat=True):
        past[rc] = past.get(rc, 0) + 1
    assert classes["life_support"]["overdue"] == past.get(RiskClass.LIFE_SUPPORT, 0), name
    assert classes["medium"]["overdue"] == past.get(RiskClass.MEDIUM, 0) + 1, name  # plus the missing one

    for wo in WorkOrder.objects.filter(type=WoType.PM, status=WoStatus.OPEN):
        assert completes_late(wo, TODAY, w) == (TODAY > w.end(wo.due_on, wo.asset.device_model.risk_class)), (name, wo.due_on)
