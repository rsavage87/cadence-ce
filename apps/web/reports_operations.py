"""Presenters for the operations reports: chart geometry and display values built from apps.reports.operations's data."""
from . import charts
from .overview import pct_label


def present_tech(r: dict) -> dict:
    """The mock's horizontal bars of work orders closed per technician, in the table's order, and the PM target as a label."""
    techs = r["technicians"]
    target = pct_label(r["pm_target"])
    if not techs:
        return {"chart": None, "pm_target": target}
    return {"chart": charts.hbars([(t["technician"].name, t["closed"]) for t in techs], fmt=lambda v: f"{v:.0f}", rh=28), "pm_target": target}


def present_recall(r: dict) -> dict:
    return {}
