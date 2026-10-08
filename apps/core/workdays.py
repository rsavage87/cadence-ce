"""
Work days as 21 CFR 803.3 counts them (slice 28, device incidents): Monday through Friday, except Federal holidays. A user facility's
report of a device-related death or serious injury is due "no more than 10 work days after the day that you become aware"
(803.30): `add_work_days(aware_on, 10)`.

The Federal holidays are the eleven of 5 U.S.C. 6103(a), each on the day it is observed: one falling on a Saturday is observed the
Friday before, one on a Sunday the Monday after (Executive Order 11582), so New Year's Day on a Saturday is observed on December 31
of the year before. Juneteenth from 2021. Not counted: Inauguration Day (a holiday only around Washington, D.C.) and days a
President gives by executive order, which nobody can know in advance; a deadline they move is a day later, never earlier.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from functools import lru_cache

JUNETEENTH_FROM = 2021


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Monday 0) of the month; n = -1 is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _holidays_of(year: int) -> list[date]:
    """The year's eleven holidays on their own dates (before moving a weekend one to the day observed)."""
    days = [
        date(year, 1, 1),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Birthday of Martin Luther King, Jr.
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        date(year, 7, 4),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        date(year, 11, 11),  # Veterans Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        date(year, 12, 25),  # Christmas Day
    ]
    if year >= JUNETEENTH_FROM:
        days.append(date(year, 6, 19))  # Juneteenth National Independence Day
    return days


@lru_cache(maxsize=64)
def federal_holidays(year: int) -> frozenset[date]:
    """The days observed as Federal holidays in `year` (next year's New Year's Day included when it is observed on December 31)."""
    observed = {_observed(d) for y in (year, year + 1) for d in _holidays_of(y)}
    return frozenset(d for d in observed if d.year == year)


def is_work_day(day: date) -> bool:
    return day.weekday() < 5 and day not in federal_holidays(day.year)


def add_work_days(day: date, n: int) -> date:
    """The n-th work day after `day` (`day` itself never counts, work day or not). n >= 1."""
    if n < 1:
        raise ValueError("n must be at least 1")
    current = day
    while n:
        current += timedelta(days=1)
        if is_work_day(current):
            n -= 1
    return current


def work_days_between(start: date, end: date) -> int:
    """How many work days after `start` up to and including `end`: 0 when end <= start. Work days left until a due date:
    work_days_between(today, due_on)."""
    count, current = 0, start
    while current < end:
        current += timedelta(days=1)
        if is_work_day(current):
            count += 1
    return count
