"""
All facilities over the API (slice 22): the figures of the web page of that name (apps.reports.all_facilities) for every facility the
signed-in person has joined. Session only (PersonPermission): a token is one facility's account and never reads another facility.

GET  /api/v1/overview/all-facilities/
     {"facilities": [...], "totals": {...} or null}. Each facility: name, slug, current (this session's facility), overview (whether
     the person's account there may see its Overview: Reports View and a role that sees the whole facility), today and month
     ("2026-10": its own, on its own clock), and the Overview's figures for that month to date: active_devices, open_work_orders,
     overdue_work_orders, awaiting_parts, pm_due, pm_on_time, pm_rate, life_support_pm_due, life_support_pm_on_time,
     life_support_pm_rate, repairs_closed, mttr_days, repair_spend, downtime_days, device_days, uptime_pct, cost_of_service (a
     year), acquisition_value, cost_of_service_ratio_pct, alerts_needing_action. A facility whose Overview the person may not see
     has every figure null. Floats to two decimals. The totals ("facilities": how many were added up) sum the facilities with
     figures and work every rate out from the sums; alerts_needing_action counts each recall alert once, since alerts are shared.
     Null when no facility has figures.
"""
from rest_framework.response import Response

from apps.reports.all_facilities import FIGURES, all_facilities

from .base import ApiViewSet
from .permissions import PersonPermission


def _value(v):
    return round(v, 2) if isinstance(v, float) else v


def _facility(row: dict) -> dict:
    shown = row["overview"]
    return {"name": row["name"], "slug": row["slug"], "current": row["current"], "overview": shown,
            "today": row["today"] if shown else None, "month": f"{row['month']:%Y-%m}" if shown else None,
            **{key: _value(row[key]) if shown else None for key in FIGURES}}


def _totals(t: dict | None) -> dict | None:
    return {"facilities": t["facilities"], **{key: _value(t[key]) for key in FIGURES}} if t else None


class AllFacilitiesViewSet(ApiViewSet):
    permission_classes = [PersonPermission]
    scoped_actions = frozenset({"list"})  # each facility is read with the person's account there, never with this one's share

    def list(self, request):
        data = all_facilities(request.user, request.tenant)
        return Response({"facilities": [_facility(r) for r in data["facilities"]], "totals": _totals(data["totals"])})


def register(router):
    router.register("overview/all-facilities", AllFacilitiesViewSet, basename="all-facilities")
