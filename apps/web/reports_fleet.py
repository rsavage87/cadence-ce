"""Presenters for the fleet reports: chart geometry and display values built from apps.reports.fleet's data."""
from apps.pm.services import pm_on_time_series

from .overview import pct_label, pm_trend_chart


def present_compliance(r: dict) -> dict:
    """The Overview's 12-month PM trend, ending at the report's month (the mock draws the same chart under this table). The series
    follow the report's clock and its PM window (slice 27: read once, with the report), so the chart and the table describe the same day
    by the same rule; the target line is the policy target the table uses."""
    today, w = r["today"], r.get("w")
    return {"chart": pm_trend_chart(pm_on_time_series(today.year, today.month, today=today, w=w),
                                    pm_on_time_series(today.year, today.month, life_support_only=True, today=today, w=w),
                                    target=r["policy_target_pct"]),
            "policy_target": pct_label(r["policy_target_pct"])}


def present_mtbf(r: dict) -> dict:
    shown = len(r["models"])
    return {"shown": shown, "truncated": r["total_models"] > shown}


def present_replace(r: dict) -> dict:
    return {"shown": len(r["top"]), "fallback_count": r["fallback_count"]}
