"""Presenters for the cost reports: chart geometry and display values built from apps.reports.cost's data."""
from . import charts
from .overview import MONTHS
from .templatetags.web import money_k


def _month_label(year: int, month: int, first: bool) -> str:
    return MONTHS[month - 1] + (f" '{year % 100:02d}" if first or month == 1 else "")


def present_cosr(r: dict) -> dict:
    # Only categories with a ratio get a bar; those with no recorded acquisition value are named in the template's hint instead.
    items = [(c["label"], c["ratio_pct"]) for c in r["categories"] if c["ratio_pct"] is not None]
    if not items:
        return {"chart": None}
    return {"chart": charts.hbars(items, fmt=lambda v: f"{v:.1f}%", rh=28, marker=r["benchmark"], marker_label="Benchmark")}


def present_spend(r: dict) -> dict:
    if not r["six_months"]:
        return {"chart": None}  # the template shows its empty state instead
    labels = [_month_label(x["year"], x["month"], i == 0) for i, x in enumerate(r["months"])]
    series = [{"name": "Labor", "color": "var(--accent)", "values": [round(x["labor"]) for x in r["months"]]},
              {"name": "Parts", "color": "var(--warn)", "values": [round(x["parts"]) for x in r["months"]]}]
    return {"chart": charts.stacked_bars(labels, series, fmt=money_k)}


def present_contract(r: dict) -> dict:
    f = r["fleet"]
    if not f["total"]:
        return {"donut": None}
    items = [{"label": "In-house labor and parts", "value": round(f["in_house"]), "color": "var(--accent)", "text": money_k(f["in_house"])},
             {"label": "Vendor time and materials", "value": round(f["vendor_tm"]), "color": "var(--warn)", "text": money_k(f["vendor_tm"])},
             {"label": "Service contracts", "value": round(f["contracts"]), "color": "var(--violet)", "text": money_k(f["contracts"])}]
    return {"donut": charts.donut(items, center=money_k(f["total"]), center_label="per year")}
