from datetime import date, timedelta

from apps.reports.services import overview_kpis
from apps.workorders.models import LaborLine
from apps.workorders.services import change_status, create_work_order


def test_overview_kpis_for_a_fixed_month(ctx, vent, pump):
    today = date(2026, 9, 22)
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm", opened_on=today - timedelta(days=4))
    LaborLine.objects.create(work_order=wo, hours=2, rate=82)
    change_status(wo, "in_progress", as_of=today - timedelta(days=3))
    change_status(wo, "completed", as_of=today - timedelta(days=1))
    k = overview_kpis(2026, 9, today=today)
    assert k["period"]["current"] and k["period"]["days"] == 22
    assert k["active_devices"] == 2
    assert k["repairs_closed"] == 1 and k["mttr_days"] == 3 and k["repair_spend"] == 164
    assert k["downtime_days"] == 3 and 0 < k["uptime_pct"] < 100
    assert k["cost_of_service"]["acquisition"] == 41200
    assert k["open_work_orders"] == 0
