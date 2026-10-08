"""
Slice 28: work days as 21 CFR 803.3 counts them (apps.core.workdays), checked against OPM's published Federal holiday calendars.
"""
from datetime import date

import pytest

from apps.core.workdays import add_work_days, federal_holidays, is_work_day, work_days_between

OPM = {
    2021: ["2021-01-01", "2021-01-18", "2021-02-15", "2021-05-31", "2021-06-18", "2021-07-05", "2021-09-06", "2021-10-11", "2021-11-11",
           "2021-11-25", "2021-12-24", "2021-12-31"],  # Jul 4 and Dec 25 on Sundays/Saturdays; Jan 1, 2022 (Sat) observed Dec 31
    2022: ["2022-01-17", "2022-02-21", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-10-10", "2022-11-11", "2022-11-24",
           "2022-12-26"],  # no January 1 of its own: observed Dec 31, 2021
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-11",
           "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-10-11", "2027-11-11",
           "2027-11-25", "2027-12-24", "2027-12-31"],
}


@pytest.mark.parametrize("year", sorted(OPM))
def test_federal_holidays_match_opm(year):
    assert sorted(federal_holidays(year)) == [date.fromisoformat(d) for d in OPM[year]]


def test_no_juneteenth_before_2021():
    assert not any(d.month == 6 for d in federal_holidays(2020))


def test_weekends_and_holidays_are_not_work_days():
    assert is_work_day(date(2026, 10, 8))  # a Thursday
    assert not is_work_day(date(2026, 10, 10))  # Saturday
    assert not is_work_day(date(2026, 10, 12))  # Columbus Day


def test_ten_work_days_after_the_day_of_becoming_aware():
    # Aware Thu Oct 8, 2026: Fri 9, (Mon 12 Columbus Day), Tue 13 .. Fri 16, Mon 19 .. Thu 22 -> the tenth is Fri Oct 23.
    assert add_work_days(date(2026, 10, 8), 10) == date(2026, 10, 23)
    # Aware on a Saturday: the day itself never counts, nor Sunday.
    assert add_work_days(date(2026, 10, 10), 1) == date(2026, 10, 13)
    # Across Thanksgiving and Christmas.
    assert add_work_days(date(2026, 11, 20), 10) == date(2026, 12, 7)
    assert add_work_days(date(2026, 12, 18), 10) == date(2027, 1, 5)


def test_work_days_left():
    assert work_days_between(date(2026, 10, 8), date(2026, 10, 23)) == 10
    assert work_days_between(date(2026, 10, 23), date(2026, 10, 23)) == 0
    assert work_days_between(date(2026, 10, 26), date(2026, 10, 23)) == 0
    with pytest.raises(ValueError):
        add_work_days(date(2026, 10, 8), 0)
