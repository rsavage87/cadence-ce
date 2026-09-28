"""
Cost reports (slice 7): cost of service ratio by category, repair spend trend, contract vs in-house.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.
"""
from datetime import date


def report_cosr(today: date) -> dict:
    return {"columns": [], "rows": []}


def report_spend(today: date) -> dict:
    return {"columns": [], "rows": []}


def report_contract(today: date) -> dict:
    return {"columns": [], "rows": []}
