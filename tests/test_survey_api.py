"""
The survey binder over the API (slice 25, part E: apps/api/views_survey.py): GET /api/v1/survey/ (the binder, tables without rows) and
GET /api/v1/survey/<section>/ (one section with its rows), with the screen's doors and words. Built against fake sections; access per
role and the requester-text canary over the real sections are tests/test_survey_access.py.
"""
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

import pytest
from survey_helpers import fake_sections

from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, WORK_ORDER, Figure, Gap, Section, Table

TODAY = date(2026, 10, 7)
API = "/api/v1/survey/"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("apps.api.views_survey.timezone.localdate", lambda: TODAY)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def maintenance(rows: int = 3) -> Section:
    values = [[f"WO-26-{i:04d}", f"CE-{10000 + i}", date(2026, 3, 1), Decimal("12.50"), 3.14159, None, True,
               datetime(2026, 3, 1, 12, 30, tzinfo=dt_timezone.utc)] for i in range(rows)]
    return Section(key="maintenance", title="Scheduled maintenance (PM) completion", topic="Whether PMs were done by their due dates",
                   covers="Jan 1, 2026 to Jun 30, 2026",
                   figures=[Figure("PMs counted", 40, "in the period"), Figure("Rate", 91.234), Figure("Budget", Decimal("1200.00")),
                            Figure("Last PM", date(2026, 6, 2))],
                   gaps=[Gap(CHECK, "Policy text says a month", "/settings/", "Settings"), Gap(FINDING, "Medium risk below target"),
                         Gap(GAP, "WO-26-0001 was done 12 days late with no reason recorded", "/work-orders/WO-26-0001/", "WO-26-0001")],
                   tables=[Table("late", "PMs not on time", ["Work order", "Device", "Due", "Cost", "Rate", "Why late", "Imported", "At"],
                                 lambda: iter(values), count=rows, links={0: WORK_ORDER, 1: DEVICE}),
                           Table("unknown", "Rows the section does not count", ["Tag"], lambda: iter([["A"], ["B"]]))],
                   notes=["On time means completed on or before the due date."])


def test_the_binder_lists_every_section_without_rows(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance()})
    signed_in("director")
    r = client.get(f"{API}?from=2026-01-01&to=2026-06-30")
    assert r.status_code == 200
    data = r.json()
    assert data["period"] == {"from": "2026-01-01", "to": "2026-06-30", "today": "2026-10-07", "label": "Jan 1, 2026 to Jun 30, 2026"}
    assert data["complete"] is True and data["left_out"] == [] and data["counts"] == {"gap": 1, "finding": 1, "check": 1}
    assert [s["key"] for s in data["sections"]] == ["program", "inventory", "maintenance", "aem", "inspections", "recalls", "staff"]
    m = next(s for s in data["sections"] if s["key"] == "maintenance")
    assert m["title"] == "Scheduled maintenance (PM) completion" and m["topic"] == "Whether PMs were done by their due dates"
    assert m["covers"] == "Jan 1, 2026 to Jun 30, 2026" and m["notes"] == ["On time means completed on or before the due date."]
    assert m["figures"] == [{"label": "PMs counted", "value": 40, "hint": "in the period"}, {"label": "Rate", "value": 91.23, "hint": ""},
                            {"label": "Budget", "value": "1200.00", "hint": ""}, {"label": "Last PM", "value": "2026-06-02", "hint": ""}]
    # gaps first, then findings, then checks, as the screen lists them; a url names the facility
    assert m["gaps"] == [
        {"kind": "gap", "text": "WO-26-0001 was done 12 days late with no reason recorded", "record": "WO-26-0001",
         "url": "/work-orders/WO-26-0001/?facility=riverside"},
        {"kind": "finding", "text": "Medium risk below target", "record": "", "url": ""},
        {"kind": "check", "text": "Policy text says a month", "record": "Settings", "url": "/settings/?facility=riverside"}]
    assert m["tables"] == [{"key": "late", "title": "PMs not on time", "columns": ["Work order", "Device", "Due", "Cost", "Rate", "Why late", "Imported", "At"],
                            "count": 3},
                           {"key": "unknown", "title": "Rows the section does not count", "columns": ["Tag"], "count": None}]


def test_the_default_period_is_the_twelve_months(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {})
    signed_in("analyst")
    data = client.get(API).json()
    assert data["period"]["from"] == "2025-11-01" and data["period"]["to"] == "2026-10-07"
    assert data["complete"] is False and data["left_out"] == [
        {"key": "staff", "title": "Technician qualifications", "reason": "Needs Users and access View, which your role does not have."}]
    assert "staff" not in [s["key"] for s in data["sections"]]


def test_a_section_comes_with_its_rows(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance()})
    signed_in("director")
    r = client.get(f"{API}maintenance/?from=2026-01-01&to=2026-06-30")
    assert r.status_code == 200
    data = r.json()
    assert data["period"]["label"] == "Jan 1, 2026 to Jun 30, 2026" and data["key"] == "maintenance" and len(data["gaps"]) == 3
    late = data["tables"][0]
    assert late["count"] == 3 and late["truncated"] is False and len(late["rows"]) == 3
    # dates ISO, Decimals as text, floats to two decimals, times in the facility's zone (New York by default)
    assert late["rows"][0] == ["WO-26-0000", "CE-10000", "2026-03-01", "12.50", 3.14, None, True, "2026-03-01T07:30-05:00"]
    assert data["tables"][1] == {"key": "unknown", "title": "Rows the section does not count", "columns": ["Tag"], "count": 2,
                                 "rows": [["A"], ["B"]], "truncated": False}


def test_a_long_table_is_truncated(client, signed_in, monkeypatch):
    monkeypatch.setattr("apps.api.views_survey.MAX_ROWS", 2)
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=5)})
    signed_in("director")
    late = client.get(f"{API}maintenance/").json()["tables"][0]
    assert late["truncated"] is True and len(late["rows"]) == 2 and late["count"] == 5
    monkeypatch.setattr("apps.api.views_survey.MAX_ROWS", 5)
    late = client.get(f"{API}maintenance/").json()["tables"][0]
    assert late["truncated"] is False and len(late["rows"]) == 5


def test_refusals_are_in_words(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance()})
    signed_in("technician")
    r = client.get(f"{API}staff/")
    assert r.status_code == 403 and r.json() == {"detail": "Needs Users and access View, which your role does not have."}
    assert client.get(f"{API}nope/").status_code == 404
    r = client.get(f"{API}?to=2026-10-08")
    assert r.status_code == 400 and r.json() == {"to": ["The binder cannot cover days after today."]}
    r = client.get(f"{API}maintenance/?from=2026-05-01&to=2026-04-01")
    assert r.status_code == 400 and r.json() == {"from": ["The period starts after it ends."]}
    assert client.get(f"{API}maintenance/?from=junk").json() == {"from": ["Enter a date as YYYY-MM-DD."]}


def test_a_token_reads_the_binder_of_its_own_facility(client, make_user, monkeypatch):
    from rest_framework.authtoken.models import Token

    fake_sections(monkeypatch, {"maintenance": maintenance()})
    token = Token.objects.create(user=make_user("manager"))
    r = client.get(API, HTTP_AUTHORIZATION=f"Token {token.key}")
    assert r.status_code == 200 and r.json()["complete"] is True
    assert client.get(f"{API}maintenance/", HTTP_AUTHORIZATION=f"Token {token.key}").status_code == 200
