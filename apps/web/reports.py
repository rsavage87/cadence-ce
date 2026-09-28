"""
Presentation for the Reports screen: chart geometry and display values per report. The numbers come from
apps.reports (cost.py, fleet.py, operations.py); the presenters sit next to them in reports_cost.py,
reports_fleet.py, and reports_operations.py and return what the report's template needs beyond the data itself.
"""


def present(key: str, r: dict) -> dict:
    from . import reports_cost, reports_fleet, reports_operations

    presenters = {"cosr": reports_cost.present_cosr, "spend": reports_cost.present_spend, "contract": reports_cost.present_contract,
                  "compliance": reports_fleet.present_compliance, "mtbf": reports_fleet.present_mtbf, "replace": reports_fleet.present_replace,
                  "tech": reports_operations.present_tech, "recall": reports_operations.present_recall}
    return presenters[key](r)
