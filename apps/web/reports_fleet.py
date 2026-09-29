"""Presenters for the fleet reports: chart geometry and display values built from apps.reports.fleet's data."""
from apps.pm.services import pm_on_time_series

from .overview import pct_label, pm_trend_chart


def present_compliance(r: dict) -> dict:
    """The Overview's 12-month PM trend, ending at the report's month (the mock draws the same chart under this table). The series
    follow the report's clock, so the chart and the table describe the same day; the target line is the policy target the table uses."""
    today = r["today"]
    return {"chart": pm_trend_chart(pm_on_time_series(today.year, today.month, today=today),
                                    pm_on_time_series(today.year, today.month, life_support_only=True, today=today), target=r["policy_target_pct"]),
            "policy_target": pct_label(r["policy_target_pct"])}


def present_mtbf(r: dict) -> dict:
    shown = len(r["models"])
    return {"shown": shown, "truncated": r["total_models"] > shown}


def present_replace(r: dict) -> dict:
    return {"shown": len(r["top"]), "fallback_count": r["fallback_count"]}
