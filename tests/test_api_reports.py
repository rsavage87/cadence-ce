"""Slice 19, part C: custom reports, report emails, and Check FDA feed over the API (apps/api/views_reports.py and views_recalls.py).

Every endpoint by session and by token, with the Reports and Recalls screens' doors (apps/reports/permissions.py,
apps/recalls/permissions.py): Reports View runs custom reports, Reports Edit builds them only on what the builder can see, running one
needs View on what it lists, report emails are only ever the requesting user's own, and Check FDA feed is at the screen's level and
shares its one-fetch-per-15-minutes limit. Every refusal (levels, scoped users, another facility's ids, bad bodies) and that each answer
is what the screen's service produces. The facility is tests/test_custom_reports.py's `world`. No test reaches openFDA:
tests/test_check_feeds.py's `openfda` fixture, autouse and imported here, answers every request and fails a test that did not expect one.
"""
from datetime import date, datetime

import pytest
import requests
from django.core.exceptions import ValidationError
from django.db.models import F
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token
from test_check_feeds import PUMP, WIDGET, openfda  # noqa: F401
from test_custom_reports import TODAY, world  # noqa: F401

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.jobs.models import JobRun
from apps.recalls import feeds
from apps.recalls.models import Alert, AlertMatch
from apps.reports import custom
from apps.reports import subscriptions as subs
from apps.reports.models import CustomReport, ReportSubscription
from apps.reports.services import REPORT_KEYS, run_any
from apps.tenants.context import tenant_context
from apps.workorders.services import create_work_order

CUSTOM = "/api/v1/custom-reports/"
EMAILS = "/api/v1/report-emails/"
FEED = "/api/v1/alert-matches/check-feed/"
HX = {"HTTP_HX_REQUEST": "true"}
TUE = date(2026, 10, 6)  # the report emails' clock: the next Monday is Oct 12, the next first Monday Nov 2
GOOD = {"name": "Repair cost", "source": "work_orders", "columns": ["number", "department", "total_cost"], "filters": {"type": ["repair"]},
        "group_by": "", "sort": "-total_cost"}
PART_OF = "part of this facility"  # ModulePermission's refusal of a scoped user


@pytest.fixture(autouse=True)
def _clocks(monkeypatch, freeze_today):
    monkeypatch.setattr("apps.api.views_reports._today", lambda: TODAY)
    freeze_today(TODAY)  # the screen's
    monkeypatch.setattr(subs, "local_today", lambda: TUE)


def url(report, action=""):
    return f"{CUSTOM}{report.pk}/{action}"


def auth(token) -> dict:
    return {"HTTP_AUTHORIZATION": f"Token {token.key}"}


def send(client, method, path, body=None, **extra):
    if method in ("get", "delete"):
        return getattr(client, method)(path, **extra)
    return getattr(client, method)(path, {} if body is None else body, content_type="application/json", **extra)


@pytest.fixture
def person(client, make_user):
    """Sign in as a default role, with an email address (make_user leaves it blank) unless email=False."""

    def _as(slug, email=True, username=None):
        user = make_user(slug, username=username)
        if email:
            user.email = user.username
            user.save(update_fields=["email"])
        client.force_login(user)
        return user

    return _as


def role_user(tenant, slug, levels, scope="", **fields):
    """A user of a custom role (inside the tenant's context)."""
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=role.tenant, role=role,
                                    email=f"{slug}@riverside.example", **fields)


def plain(value):
    """A value as the API's JSON carries it: dates as ISO text, floats to two decimals."""
    if isinstance(value, date):
        return value.isoformat()
    return round(value, 2) if isinstance(value, float) else value


def when(text) -> datetime:
    return datetime.fromisoformat(text)


# --- custom reports: reading --------------------------------------------------------------------------------------------------

def test_the_list_and_one_report_say_what_the_screen_says(client, person, world):  # noqa: F811
    zeta = custom.create_custom_report(name="zeta fleet", source="devices", columns=["status", "tag"], filters={"department": [str(world["icu"].pk)]})
    alpha = custom.create_custom_report(name="Alpha labor", source="labor", columns=["hours", "who"], group_by="who", sort="-hours")
    person("technician")  # Reports View
    r = client.get(CUSTOM)
    assert r.status_code == 200
    assert r.json() == [  # by name in any letter case, as the screen's list
        {"id": str(alpha.pk), "key": alpha.key, "name": "Alpha labor", "source": "labor", "source_label": "Labor (time logged)",
         "description": custom.meta_of(alpha)["subtitle"]},
        {"id": str(zeta.pk), "key": zeta.key, "name": "zeta fleet", "source": "devices", "source_label": "Devices",
         "description": "Devices · Department: ICU"},
    ]
    assert r.json()[0]["description"].startswith("Labor (time logged) · Grouped by ")
    one = client.get(url(zeta)).json()
    assert one == {**r.json()[1], "columns": ["tag", "status"], "filters": {"department": [str(world["icu"].pk)]}, "group_by": "", "sort": ""}


def test_a_report_shows_its_definition_as_it_runs_and_a_patch_keeps_it(client, person, world):  # noqa: F811
    """A definition saved before a column was dropped shows (and keeps, on a PATCH of another part) what it runs: the builder's Edit."""
    stale = CustomReport.objects.create(name="Old columns", source="work_orders", columns=["number", "dropped_column"], filters={"dropped": ["x"]},
                                        sort="-dropped")
    person("analyst")
    shown = client.get(url(stale)).json()
    assert (shown["columns"], shown["filters"], shown["sort"]) == (["number"], {}, "")
    r = send(client, "patch", url(stale), {"name": "Numbers"})
    assert r.status_code == 200, r.content
    stale.refresh_from_db()
    assert (stale.name, stale.columns, stale.filters, stale.sort) == ("Numbers", ["number"], {}, "")
    gone = CustomReport.objects.create(name="No source", source="contracts", columns=["reference"])
    assert client.get(url(gone)).json() == {"id": str(gone.pk), "key": gone.key, "name": "No source", "source": "contracts", "source_label": "",
                                            "description": "Its source is no longer offered", "columns": ["reference"], "filters": {},
                                            "group_by": "", "sort": ""}
    ran = client.get(url(gone, "run/")).json()
    assert ran["rows"] == [] and "source is no longer offered" in ran["problem"]


# --- custom reports: building ---------------------------------------------------------------------------------------------------

def test_adding_one_saves_what_the_builder_saves(client, person, world):  # noqa: F811
    kim = person("analyst")
    body = {"name": "  Repair   cost by department ", "source": "work_orders", "columns": ["total_cost", "number", "department"],
            "filters": {"type": ["repair"], "date": {"field": "completed", "period": "last_30"}}, "group_by": "", "sort": "-total_cost"}
    r = send(client, "post", CUSTOM, body)
    assert r.status_code == 201, r.content
    report = CustomReport.objects.get()
    assert r.json() == client.get(url(report)).json() and r.json()["key"] == report.key
    assert report.name == "Repair cost by department" and report.created_by == kim
    clean = custom.clean_definition(**{k: body[k] for k in ("source", "columns", "filters", "group_by", "sort")})
    assert custom.definition_of(report) == clean and report.columns == ["number", "department", "total_cost"]  # the registry's order
    added = report.history.get()
    assert (added.history_user, added.history_change_reason) == (kim, "Added")


def test_a_form_body_works_for_the_flat_parts(client, person, world):  # noqa: F811
    person("analyst")
    r = client.post(CUSTOM, {"name": "Fleet", "source": "devices", "columns": ["tag", "status"], "sort": "tag"})
    assert r.status_code == 201, r.content
    assert r.json()["columns"] == ["tag", "status"] and r.json()["sort"] == "tag"


def test_a_patch_changes_only_what_it_sends(client, person, world):  # noqa: F811
    kim = person("analyst")
    report = custom.create_custom_report(name="Repairs", source="work_orders", columns=["number", "department", "total_cost"],
                                         filters={"type": ["repair"]}, sort="-total_cost", by=kim)
    r = send(client, "patch", url(report), {"name": "Repairs, all time"})
    assert r.status_code == 200 and r.json()["name"] == "Repairs, all time"
    report.refresh_from_db()
    assert (report.columns, report.filters, report.sort) == (["number", "department", "total_cost"], {"type": ["repair"]}, "-total_cost")
    assert report.history.first().history_change_reason == "Edited: name" and report.history.first().history_user == kim
    r = send(client, "patch", url(report), {"group_by": "department", "sort": "-count"})
    assert r.status_code == 200 and (r.json()["group_by"], r.json()["sort"]) == ("department", "-count")
    r = send(client, "patch", url(report), {"source": "devices", "columns": ["tag"], "filters": {}, "group_by": "", "sort": ""})
    assert r.status_code == 200 and r.json()["source"] == "devices" and r.json()["description"] == "Devices"
    count = report.history.count()
    assert send(client, "patch", url(report), {}).status_code == 200 and report.history.count() == count  # nothing changed, nothing saved
    got = client.get(url(report)).json()
    r = send(client, "patch", url(report), {**got, "name": "Fleet"})  # what GET returned, sent back with a change
    assert r.status_code == 200 and r.json() == {**got, "name": "Fleet"}
    assert send(client, "put", url(report), got).status_code == 405


def test_deleting_one_removes_its_email_schedules(client, person, world):  # noqa: F811
    kim = person("analyst")
    report = custom.create_custom_report(name="Fleet", source="devices", columns=["tag"])
    subs.set_subscription(kim, report.key, "weekly")
    r = send(client, "delete", url(report))
    assert r.status_code == 200
    assert r.json() == {"id": str(report.pk), "key": report.key, "name": "Fleet", "deleted": True, "email_schedules_removed": 1}
    assert not CustomReport.objects.exists() and not ReportSubscription.objects.exists()
    assert send(client, "delete", url(report)).status_code == 404


BAD = [
    ({"source": "contracts"}, "source"),
    ({"source": ["work_orders"]}, "source"),
    ({"columns": ["problem"]}, "columns"),
    ({"columns": []}, "columns"),
    ({"columns": None}, "columns"),
    ({"filters": ["type"]}, "filters"),
    ({"filters": {"requester": ["RN Lee"]}}, "filters"),
    ({"filters": {"type": ["nope"]}}, "filter_type"),
    ({"filters": {"department": ["7d9f2c1e-0000-4000-8000-000000000000"]}}, "filter_department"),
    ({"filters": {"date": {"field": "completed", "period": "fortnight"}}}, "date"),
    ({"group_by": "requester"}, "group_by"),
    ({"sort": "priority"}, "sort"),
    ({"sort": ["number"]}, "sort"),
]


@pytest.mark.parametrize("change, part", BAD)
def test_a_definition_the_registry_refuses_is_a_400_keyed_by_part_in_its_words(client, person, world, change, part):  # noqa: F811
    person("analyst")
    definition = {k: v for k, v in {**GOOD, **change}.items() if k != "name"}
    with pytest.raises(ValidationError) as refused:
        custom.clean_definition(**definition)
    words = refused.value.message_dict[part]
    r = send(client, "post", CUSTOM, {**GOOD, **change})
    assert r.status_code == 400 and r.json()[part] == words, r.content
    report = custom.create_custom_report(**GOOD)
    r = send(client, "patch", url(report), change)
    assert r.status_code == 400 and r.json()[part] == words, r.content
    assert CustomReport.objects.get() == report and custom.definition_of(report) == custom.definition_of(CustomReport.objects.get())


def test_names_are_refused_in_the_builders_words(client, person, world):  # noqa: F811
    person("analyst")
    assert send(client, "post", CUSTOM, {**GOOD, "name": "  "}).json() == {"name": ["Name the report."]}
    assert send(client, "post", CUSTOM, {**GOOD, "name": None}).json() == {"name": ["Name the report."]}
    assert send(client, "post", CUSTOM, {**GOOD, "name": ["Fleet"]}).json() == {"name": ["Enter the name as text."]}
    assert send(client, "post", CUSTOM, {**GOOD, "name": "x" * 81}).json() == {"name": ["Keep the name to 80 characters."]}
    assert send(client, "post", CUSTOM, GOOD).status_code == 201
    r = send(client, "post", CUSTOM, {**GOOD, "name": "REPAIR COST"})
    assert r.status_code == 400 and r.json() == {"name": ["Another custom report here is already called REPAIR COST."]}
    r = send(client, "post", CUSTOM, {"name": "", "source": "work_orders", "columns": ["problem"]})  # every problem at once
    assert r.status_code == 400 and set(r.json()) == {"name", "columns"}
    assert CustomReport.objects.count() == 1


def test_unknown_fields_and_what_only_get_shows_are_refused(client, person, world):  # noqa: F811
    person("analyst")
    r = send(client, "post", CUSTOM, {**GOOD, "tenant": "x", "created_by": "y"})
    assert r.status_code == 400 and r.json() == {"detail": "Unknown fields: created_by, tenant."}
    r = send(client, "post", CUSTOM, {**GOOD, "id": "abc", "key": "custom-x"})
    assert r.status_code == 400 and set(r.json()) == {"id", "key"}
    assert send(client, "post", CUSTOM, [GOOD]).json() == {"detail": "Send a JSON object."}
    assert not CustomReport.objects.exists()
    report = custom.create_custom_report(**GOOD)
    for change in ({"key": "custom-other"}, {"description": "Mine"}, {"source_label": "Devices"}, {"id": "abc"}):
        r = send(client, "patch", url(report), {**change, "name": "Changed"})
        assert r.status_code == 400 and list(r.json()) == list(change), r.content
    assert send(client, "patch", url(report), {"name": "Changed", "requester": "x"}).json() == {"detail": "Unknown fields: requester."}
    assert CustomReport.objects.get().name == "Repair cost"


# --- custom reports: running ------------------------------------------------------------------------------------------------------

def test_running_returns_the_table_the_screen_runs(client, person, world):  # noqa: F811
    person("technician")  # Reports View, Work orders View
    listed = custom.create_custom_report(name="Recent work", source="work_orders", columns=["number", "tag", "opened", "labor_hours", "total_cost",
                                         "pm_on_time"], filters={"date": {"field": "opened", "period": "last_30"}}, sort="-total_cost")
    grouped = custom.create_custom_report(name="By department", source="work_orders", columns=["number", "department", "total_cost", "days_open",
                                          "pm_on_time"], group_by="department", sort="-total_cost")
    for report in (listed, grouped):
        expected = run_any(report.key, TODAY)
        for method in ("get", "post"):
            r = send(client, method, url(report, "run/"))
            assert r.status_code == 200, r.content
            data = r.json()
            assert (data["id"], data["key"], data["name"], data["as_of"]) == (str(report.pk), report.key, report.name, TODAY.isoformat())
            assert data["rows"] == [[plain(v) for v in row] for row in expected["rows"]] and data["rows"]
            assert data["totals"] == (None if expected["totals"] is None else [plain(v) for v in expected["totals"]])
            for key in ("columns", "kinds", "total", "records", "truncated", "grouped", "left_out", "description", "problem"):
                assert data[key] == expected[key], key
    ran = client.get(url(listed, "run/")).json()
    assert "Opened: last 30 days (Sep 3, 2026 to Oct 2, 2026)" in ran["description"] and not ran["grouped"]
    ran = client.get(url(grouped, "run/")).json()
    assert ran["grouped"] and ran["left_out"] == ["Number"] and ran["columns"][:2] == ["Department", "Count"]
    assert all(v == round(v, 2) for row in ran["rows"] for v in row if isinstance(v, float))


def test_a_run_says_when_it_lists_only_the_first_rows(client, person, world, monkeypatch):  # noqa: F811
    person("analyst")
    report = custom.create_custom_report(name="All work", source="work_orders", columns=["number"])
    monkeypatch.setattr(custom, "MAX_ROWS", 2)
    data = client.get(url(report, "run/")).json()
    assert data["truncated"] and len(data["rows"]) == 2 and data["total"] == run_any(report.key, TODAY)["total"] > 2


# --- custom reports: who may do what ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["manager", "technician"])
def test_reports_view_runs_custom_reports_but_never_builds_them(client, person, world, role):  # noqa: F811
    report = custom.create_custom_report(**GOOD)
    person(role)
    for method, path in [("get", CUSTOM), ("get", url(report)), ("get", url(report, "run/")), ("post", url(report, "run/"))]:
        assert send(client, method, path).status_code == 200, (method, path)
    for method, path, body in [("post", CUSTOM, {**GOOD, "name": "Mine"}), ("patch", url(report), {"name": "Mine"}), ("delete", url(report), None)]:
        r = send(client, method, path, body)
        assert r.status_code == 403, (method, path)
    assert CustomReport.objects.get().name == "Repair cost"


def test_a_builder_builds_only_on_what_they_can_see(client, tenant, world):  # noqa: F811
    builder = role_user(tenant, "fleet-builder", {Module.REPORTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    client.force_login(builder)
    r = send(client, "post", CUSTOM, GOOD)
    assert r.status_code == 403 and r.json() == {"detail": "You need Work orders View to build a report that lists work orders."}
    r = send(client, "post", CUSTOM, {"name": "Time", "source": "labor", "columns": ["hours"]})
    assert r.status_code == 403 and r.json() == {"detail": "You need Work orders View to build a report that lists work orders' labor."}
    assert not CustomReport.objects.exists()
    r = send(client, "post", CUSTOM, {"name": "Fleet", "source": "devices", "columns": ["tag"]})
    assert r.status_code == 201
    fleet = CustomReport.objects.get(name="Fleet")
    repairs = custom.create_custom_report(**GOOD)
    # As the builder's Edit: a report on what they cannot see is not theirs to change, nor is a report of theirs moved onto it.
    r = send(client, "patch", url(repairs), {"name": "Mine now"})
    assert r.status_code == 403 and r.json()["detail"] == "You need Work orders View to build a report that lists work orders."
    r = send(client, "patch", url(fleet), {"source": "parts", "columns": ["amount"]})
    assert r.status_code == 403 and r.json()["detail"] == "You need Work orders View to build a report that lists work orders' parts."
    # Running it is refused in the panel's and the CSV's words; deleting needs only Reports Edit, as on the screen.
    r = client.get(url(repairs, "run/"))
    words = "This report lists work orders, so it needs Work orders View, which your role does not have."
    assert r.status_code == 403 and r.json() == {"detail": words}
    csv = client.get(f"/reports/{repairs.key}.csv")
    assert csv.status_code == 403 and csv.content.decode() == words
    assert client.get(url(repairs)).status_code == 200
    assert send(client, "delete", url(repairs)).status_code == 200
    assert list(CustomReport.objects.values_list("name", "source")) == [("Fleet", "devices")]


def test_another_facilitys_report_is_not_found(client, person, world, other_tenant):  # noqa: F811
    with tenant_context(other_tenant):
        theirs = custom.create_custom_report(name="Theirs", source="devices", columns=["tag"])
    person("director")
    for method, path in [("get", url(theirs)), ("patch", url(theirs)), ("delete", url(theirs)), ("get", url(theirs, "run/")),
                         ("post", url(theirs, "run/")), ("get", f"{CUSTOM}not-an-id/"), ("get", f"{CUSTOM}not-an-id/run/")]:
        assert send(client, method, path, {"name": "Mine"}).status_code == 404, (method, path)
    assert client.get(CUSTOM).json() == []
    with tenant_context(other_tenant):
        assert CustomReport.objects.get().name == "Theirs"


def test_the_reports_endpoint_still_serves_only_the_eight(client, person, world):  # noqa: F811
    report = custom.create_custom_report(**GOOD)
    person("analyst")
    assert [r["key"] for r in client.get("/api/v1/reports/").json()] == REPORT_KEYS
    assert client.get(f"/api/v1/reports/{report.key}/").status_code == 404


def test_the_overview_refuses_a_month_it_cannot_read(client, person, ctx):
    person("analyst")
    assert client.get("/api/v1/overview/?y=2026&m=9").json()["period"]["start"] == "2026-09-01"
    for query in ("y=x", "m=13", "y=2026&m=0", "y=99999"):
        r = client.get(f"/api/v1/overview/?{query}")
        assert r.status_code == 400 and ("m must be" in r.json()["detail"] or "whole numbers" in r.json()["detail"]), query


def test_custom_reports_by_token(client, tenant, make_user, world):  # noqa: F811
    token = Token.objects.create(user=make_user("director"))
    r = send(client, "post", CUSTOM, GOOD, **auth(token))
    assert r.status_code == 201, r.content
    report = CustomReport.unscoped.get(pk=r.json()["id"])  # unscoped: which facility the row landed in
    assert report.tenant_id == tenant.id and report.created_by == token.user
    assert [x["key"] for x in client.get(CUSTOM, **auth(token)).json()] == [report.key]
    assert client.get(url(report), **auth(token)).json()["name"] == "Repair cost"
    assert send(client, "patch", url(report), {"sort": "number"}, **auth(token)).json()["sort"] == "number"
    ran = send(client, "post", url(report, "run/"), None, **auth(token)).json()
    assert ran["rows"] == [[plain(v) for v in row] for row in run_any(report.key, TODAY)["rows"]]
    assert send(client, "delete", url(report), **auth(token)).status_code == 200 and not CustomReport.objects.exists()


# --- report emails ----------------------------------------------------------------------------------------------------------------

def test_report_emails_are_set_and_listed_as_the_schedule_button_does(client, person, world):  # noqa: F811
    kim = person("analyst")
    assert client.get(EMAILS).json() == []
    r = send(client, "put", EMAILS, {"report": "cosr", "frequency": "weekly"})
    assert r.status_code == 200
    weekly = {"report": "cosr", "title": "Cost of service ratio by category", "frequency": "weekly", "frequency_label": "Every Monday",
              "next_on": "2026-10-12", "not_sent": None}
    assert r.json() == weekly and client.get(EMAILS).json() == [weekly]
    assert subs.subscription_for(kim, "cosr").frequency == "weekly"
    assert "Next email Monday, October 12, 2026" in client.get("/reports/cosr/schedule/", **HX).content.decode()  # the modal agrees
    r = send(client, "post", EMAILS, {"report": "cosr", "frequency": "monthly"})
    assert r.json() == {**weekly, "frequency": "monthly", "frequency_label": "First Monday of each month", "next_on": "2026-11-02"}
    fleet = custom.create_custom_report(name="Fleet", source="devices", columns=["tag"])
    r = send(client, "put", EMAILS, {"report": f"custom-{str(fleet.pk).upper()}", "frequency": "weekly"})
    assert r.status_code == 200 and (r.json()["report"], r.json()["title"]) == (fleet.key, "Fleet")
    assert [e["report"] for e in client.get(EMAILS).json()] == ["cosr", fleet.key]
    off = {"report": "cosr", "title": "Cost of service ratio by category", "frequency": "", "frequency_label": "Off", "next_on": None, "not_sent": None}
    assert send(client, "put", EMAILS, {"report": "cosr", "frequency": ""}).json() == off
    assert subs.subscription_for(kim, "cosr") is None
    assert send(client, "put", EMAILS, {"report": "cosr", "frequency": None}).json() == off  # off when it is off already is fine
    assert [e["report"] for e in client.get(EMAILS).json()] == [fleet.key]


def test_report_emails_are_only_ever_the_users_own(client, person, make_user, world):  # noqa: F811
    boss = make_user("director")
    boss.email = "boss@riverside.example"
    boss.save(update_fields=["email"])
    subs.set_subscription(boss, "spend", "weekly")
    kim = person("analyst")
    assert client.get(EMAILS).json() == []
    for extra in ({"user": str(boss.pk)}, {"email": "someone@elsewhere.example"}, {"user_id": boss.pk}):
        r = send(client, "put", EMAILS, {"report": "spend", "frequency": "monthly", **extra})
        assert r.status_code == 400 and r.json()["detail"].startswith(f"Unknown fields: {next(iter(extra))}."), r.content
    assert send(client, "put", EMAILS, {"report": "spend", "frequency": "monthly"}).status_code == 200
    assert subs.subscription_for(boss, "spend").frequency == "weekly" and subs.subscription_for(kim, "spend").frequency == "monthly"
    assert send(client, "put", EMAILS, {"report": "spend", "frequency": ""}).status_code == 200
    assert subs.subscription_for(boss, "spend").frequency == "weekly"


def test_report_email_refusals_are_400s_in_the_services_words(client, person, make_user, tenant, other_tenant, world):  # noqa: F811
    person("analyst")
    with tenant_context(other_tenant):
        theirs = custom.create_custom_report(name="Theirs", source="devices", columns=["tag"])
    cases = [
        ({"report": "nope", "frequency": "weekly"}, {"detail": "There is no such report."}),
        ({"report": theirs.key, "frequency": "weekly"}, {"detail": "There is no such report."}),
        ({"report": "cosr", "frequency": "daily"}, {"detail": "Choose Off, Every Monday, or First Monday of each month."}),
        ({"report": "cosr"}, {"frequency": ['Required: "weekly", "monthly", or "" to turn it off.']}),
        ({"report": "cosr", "frequency": 7}, {"frequency": ['Choose "weekly", "monthly", or "" to turn it off.']}),
        ({"frequency": "weekly"}, {"report": ["Required: a report's key, from /api/v1/reports/ or /api/v1/custom-reports/."]}),
        ({"report": ["cosr"], "frequency": "weekly"}, {"report": ["Required: a report's key, from /api/v1/reports/ or /api/v1/custom-reports/."]}),
    ]
    for body, expected in cases:
        r = send(client, "put", EMAILS, body)
        assert r.status_code == 400 and r.json() == expected, (body, r.content)
    assert send(client, "put", EMAILS, [{"report": "cosr", "frequency": "weekly"}]).json() == {"detail": "Send a JSON object."}
    person("technician", email=False)  # Reports View, no email address
    r = send(client, "put", EMAILS, {"report": "cosr", "frequency": "weekly"})
    assert r.status_code == 400 and r.json() == {"detail": "Your account has no email address, so there is nowhere to send the report."}
    client.force_login(role_user(tenant, "fleet-only", {Module.REPORTS: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))
    repairs = custom.create_custom_report(**GOOD)
    r = send(client, "put", EMAILS, {"report": repairs.key, "frequency": "weekly"})
    assert r.status_code == 400 and r.json() == {"detail": "You need Work orders View to have this report emailed: it lists work orders."}
    assert not ReportSubscription.objects.exists()
    person("requester", username="nurse@riverside.example")  # no Reports access
    assert client.get(EMAILS).status_code == 403
    assert send(client, "put", EMAILS, {"report": "cosr", "frequency": "weekly"}).status_code == 403


def test_a_schedule_the_daily_job_would_skip_says_why(client, tenant, world):  # noqa: F811
    user = role_user(tenant, "reader", {Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    client.force_login(user)
    repairs = custom.create_custom_report(**GOOD)
    assert send(client, "put", EMAILS, {"report": repairs.key, "frequency": "weekly"}).json()["not_sent"] is None
    user.role.set_levels({Module.WORKORDERS: Level.NONE})
    assert client.get(EMAILS).json() == [{"report": repairs.key, "title": "Repair cost", "frequency": "weekly", "frequency_label": "Every Monday",
                                          "next_on": None, "not_sent": "cannot see what the report lists"}]
    assert send(client, "put", EMAILS, {"report": repairs.key, "frequency": ""}).json()["frequency"] == ""  # turning off always works
    assert not ReportSubscription.objects.exists()


def test_report_emails_by_token(client, tenant, make_user, world):  # noqa: F811
    user = make_user("director")
    user.email = "dir@riverside.example"
    user.save(update_fields=["email"])
    token = Token.objects.create(user=user)
    r = send(client, "put", EMAILS, {"report": "spend", "frequency": "weekly"}, **auth(token))
    assert r.status_code == 200 and r.json()["next_on"] == "2026-10-12"
    sub = ReportSubscription.unscoped.get()  # unscoped: which facility and user the row landed on
    assert (sub.tenant_id, sub.user_id, sub.report) == (tenant.id, user.id, "spend")
    assert [e["report"] for e in client.get(EMAILS, **auth(token)).json()] == ["spend"]


# --- Check FDA feed -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def their_pump(other_tenant):
    """The other facility owns the same pump model."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        return DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")


def matches_of(tenant) -> list[str]:
    with tenant_context(tenant):
        return sorted(AlertMatch.objects.values_list("alert__external_id", flat=True))


def test_check_feed_does_what_the_screens_button_does(client, person, tenant, other_tenant, their_pump, pump_model, openfda):  # noqa: F811
    person("manager")  # Recalls Approve; the button is at Edit
    openfda.records(PUMP, WIDGET)
    r = send(client, "post", FEED)
    assert r.status_code == 200, r.content
    data = r.json()
    run = JobRun.objects.get(job=feeds.CHECK_JOB)
    assert when(data.pop("checked_at")) == run.started_at
    assert data == {"fetched": True, "new_notices": 2, "returned": 2, "total": 2, "truncated": False, "new_matches": 1, "again_at": None,
                    "last_failed": False, "message": "Checked the FDA recall feed: 2 new notices, 1 new match for your inventory"}
    assert openfda.calls[0]["timeout"] == feeds.CHECK_TIMEOUT and run.status == "succeeded" and "riverside: 2 new alerts" in run.output
    assert matches_of(tenant) == ["Z-0101-2026"] and matches_of(other_tenant) == []  # this facility only, as on the screen


def test_check_feed_shares_the_cooldown_with_the_screen_and_the_daily_import(client, person, pump_model, openfda):  # noqa: F811
    person("director")
    openfda.records(PUMP)
    assert send(client, "post", FEED).json()["fetched"] is True
    screen = client.post("/recalls/check-feed/", **HX)  # the screen's button inside the cooldown fetches nothing
    assert "it can be checked again at" in screen["HX-Trigger"] and len(openfda.calls) == 1
    data = send(client, "post", FEED).json()
    last = JobRun.objects.get(job=feeds.CHECK_JOB).started_at
    again = last + feeds.CHECK_COOLDOWN
    assert len(openfda.calls) == 1 and data["fetched"] is False and data["new_notices"] is None and data["new_matches"] == 0
    assert when(data["checked_at"]) == last and when(data["again_at"]) == again and data["last_failed"] is False
    assert data["message"] == f"The FDA recall feed was checked at {feeds.clock(last)}; it can be checked again at {feeds.clock(again)}. No new matches."
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    assert send(client, "post", FEED).json()["message"] == "Checked the FDA recall feed: no new alerts" and len(openfda.calls) == 2
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    assert client.post("/recalls/check-feed/", **HX).status_code == 200 and len(openfda.calls) == 3  # the screen's press, then the API's
    assert send(client, "post", FEED).json()["fetched"] is False and len(openfda.calls) == 3


def test_an_openfda_failure_is_a_502_in_the_screens_words(client, person, pump_model, openfda):  # noqa: F811
    person("director")
    openfda.response = requests.Timeout("read timed out")
    r = send(client, "post", FEED)
    assert r.status_code == 502
    assert r.json() == {"detail": "Could not check the FDA recall feed: openFDA did not answer in time. Nothing changed; the daily import will try again."}
    run = JobRun.objects.get(job=feeds.CHECK_JOB)
    assert run.status == "failed" and "riverside: openFDA did not answer in time" in run.output and not Alert.objects.exists()
    data = send(client, "post", FEED).json()  # inside the cooldown: says the last fetch failed
    assert data["fetched"] is False and data["last_failed"] is True and data["message"].startswith("The FDA recall feed did not answer at ")


@pytest.mark.parametrize("role", ["technician", "analyst", "requester", "vendor"])
def test_check_feed_needs_the_screens_level(client, person, ctx, role, openfda):  # noqa: F811
    person(role)
    assert send(client, "post", FEED).status_code == 403
    assert openfda.calls == [] and not JobRun.objects.exists()  # refused before anything is fetched or claimed


def test_check_feed_is_a_post_and_needs_signing_in(client, person, ctx, openfda):  # noqa: F811
    assert send(client, "post", FEED).status_code == 403
    person("director")
    assert client.get(FEED).status_code == 405 and openfda.calls == []


def test_check_feed_by_token(client, tenant, make_user, pump_model, openfda):  # noqa: F811
    token = Token.objects.create(user=make_user("manager"))
    openfda.records(PUMP)
    r = send(client, "post", FEED, None, **auth(token))
    assert r.status_code == 200 and r.json()["new_matches"] == 1 and matches_of(tenant) == ["Z-0101-2026"]


# --- closed to scoped users, and to no facility -------------------------------------------------------------------------------------

@pytest.mark.parametrize("scope, fields", [(DataScope.COMPANY, {"company": "Hamilton Medical"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_every_endpoint_here_refuses_a_scoped_user_whatever_their_levels(client, tenant, world, scope, fields, openfda):  # noqa: F811
    client.force_login(role_user(tenant, f"scoped-{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **fields))
    report = custom.create_custom_report(**GOOD)
    for method, path, body in [("get", CUSTOM, None), ("post", CUSTOM, {**GOOD, "name": "Mine"}), ("get", url(report), None),
                               ("patch", url(report), {"name": "Mine"}), ("delete", url(report), None), ("get", url(report, "run/"), None),
                               ("post", url(report, "run/"), None), ("get", EMAILS, None), ("put", EMAILS, {"report": "cosr", "frequency": "weekly"}),
                               ("post", EMAILS, {"report": "cosr", "frequency": "weekly"}), ("post", FEED, None)]:
        r = send(client, method, path, body)
        assert r.status_code == 403 and PART_OF in r.json()["detail"], (method, path, r.status_code)
    assert list(CustomReport.objects.values_list("name", flat=True)) == ["Repair cost"]
    assert not ReportSubscription.objects.exists() and openfda.calls == []


def test_a_superuser_with_no_facility_is_told_to_pick_one(client, db, openfda):  # noqa: F811
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    client.force_login(root)
    for method, path, body in [("get", CUSTOM, None), ("post", CUSTOM, GOOD), ("get", EMAILS, None), ("put", EMAILS, {"report": "cosr", "frequency": "weekly"}),
                               ("post", FEED, None)]:
        r = send(client, method, path, body)
        assert r.status_code == 403 and r.json()["detail"] == "Pick a tenant first (Admin, Tenants).", (method, path)
    assert openfda.calls == []


# --- PostgreSQL row-level security --------------------------------------------------------------------------------------------------

@needs_postgres
def test_by_token_under_the_policies(client, tenant, other_tenant, make_user, openfda):  # noqa: F811
    """As the runtime role, with no tenant set before the request: build, read, change, run, schedule, and delete a custom report, and
    check the FDA feed, by token. The other facility's token sees none of it."""
    create_default_roles(other_tenant)
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        create_work_order(asset=Asset.objects.create(tag="CE-10002", device_model=model, department=dept), type="repair", priority="normal",
                          problem="Occlusion alarm")
    director = make_user("director")
    director.email = director.username
    director.save(update_fields=["email"])
    mine, theirs = Token.objects.create(user=director), Token.objects.create(user=make_user("director", other_tenant))
    openfda.records(PUMP)
    as_app_role()
    r = send(client, "post", CUSTOM, {"name": "Repairs", "source": "work_orders", "columns": ["number", "tag", "department"]}, **auth(mine))
    assert r.status_code == 201, r.content
    report = r.json()
    one = f"{CUSTOM}{report['id']}/"
    assert [x["name"] for x in client.get(CUSTOM, **auth(mine)).json()] == ["Repairs"]
    assert client.get(CUSTOM, **auth(theirs)).json() == [] and client.get(one, **auth(theirs)).status_code == 404
    assert send(client, "patch", one, {"filters": {"department": [str(dept.pk)]}}, **auth(mine)).json()["description"] == "Work orders · Department: ICU"
    ran = send(client, "post", f"{one}run/", None, **auth(mine)).json()
    assert [row[1:] for row in ran["rows"]] == [["CE-10002", "ICU"]]
    assert send(client, "get", f"{one}run/", **auth(theirs)).status_code == 404
    assert send(client, "put", EMAILS, {"report": report["key"], "frequency": "weekly"}, **auth(mine)).status_code == 200
    assert [e["report"] for e in client.get(EMAILS, **auth(mine)).json()] == [report["key"]] and client.get(EMAILS, **auth(theirs)).json() == []
    assert send(client, "post", FEED, None, **auth(mine)).json()["new_matches"] == 1
    assert send(client, "delete", one, **auth(mine)).json()["email_schedules_removed"] == 1
    with tenant_context(tenant):
        assert not CustomReport.objects.exists() and not ReportSubscription.objects.exists()
        assert list(AlertMatch.objects.values_list("alert__external_id", flat=True)) == ["Z-0101-2026"]
    with tenant_context(other_tenant):
        assert not AlertMatch.objects.exists()
