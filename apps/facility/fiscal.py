"""The facility's fiscal year (slice 36): Settings' fiscal_year_start_month, 1 (January) to 12.

A fiscal year is named by the calendar year it ends: with July as the start, FY2027 runs Jul 1, 2026 to Jun 30, 2027; with January,
FY2027 is the calendar year 2027. Read the start once (`start_month()`) and pass it down with `today`; nothing stored holds a fiscal year
derived from it, so a change re-buckets what is shown and moves nothing.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def start_month() -> int:
    """This facility's fiscal year start (inside its tenant_context): Settings', January until first saved."""
    from .services import get_settings

    return get_settings().fiscal_year_start_month or 1


def fiscal_year_of(day: date, start: int) -> int:
    """The fiscal year `day` falls in, named by the calendar year it ends."""
    if start == 1:
        return day.year
    return day.year + 1 if day.month >= start else day.year


def current_fy(today: date, start: int) -> int:
    return fiscal_year_of(today, start)


def fiscal_bounds(fy: int, start: int) -> tuple[date, date]:
    """The first and last day of fiscal year `fy`."""
    if start == 1:
        return date(fy, 1, 1), date(fy, 12, 31)
    return date(fy - 1, start, 1), date(fy, start, 1) - timedelta(days=1)


def fy_label(fy: int) -> str:
    return f"FY{fy}"


def month_name(start: int) -> str:
    return calendar.month_name[start]


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def describe(fy: int, start: int) -> str:
    """ "FY2027: Jul 1, 2026 to Jun 30, 2027" (the Settings panel's example, the outlook's headings' titles)."""
    try:
        first, last = fiscal_bounds(fy, start)
    except ValueError:  # beyond the calendar (year 10000): the name alone, never an error
        return fy_label(fy)
    return f"{fy_label(fy)}: {_day(first)} to {_day(last)}"
