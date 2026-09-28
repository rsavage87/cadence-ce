"""
Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.
"""
from datetime import date


def report_compliance(today: date) -> dict:
    return {"columns": [], "rows": []}


def report_mtbf(today: date) -> dict:
    return {"columns": [], "rows": []}


def report_replace(today: date) -> dict:
    return {"columns": [], "rows": []}
