"""Presenters for the operations reports: chart geometry and display values built from apps.reports.operations's data."""
from . import charts


def present_tech(r: dict) -> dict:
    """The mock's horizontal bars of work orders closed per technician, in the table's order."""
    techs = r["technicians"]
    if not techs:
        return {"chart": None}
    return {"chart": charts.hbars([(t["technician"].name, t["closed"]) for t in techs], fmt=lambda v: f"{v:.0f}", rh=28)}


def present_recall(r: dict) -> dict:
    return {}
