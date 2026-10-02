"""Custom reports on the Reports screen (slice 18): the list under the eight, the "+ Custom report" button, the builder (a page and a
panel, the source switch, Preview, Save, errors, Edit, Delete), a custom report's panel, CSV, print page, and Schedule button, and who
may do each: Reports View runs them but never builds, a scoped user is refused, and someone without View on what a report lists is
refused in plain words. The engine is tests/test_custom_reports.py; its `world` is the facility here too."""
import json
import re

import pytest
from csvutil import csv_rows, csv_text
from pg_helpers import as_app_role, needs_postgres
from test_custom_reports import TODAY, world  # noqa: F401

from apps.accounts.models import Level, Module, Role, User
from apps.reports import custom
from apps.reports import subscriptions as subs
from apps.reports.models import CustomReport, ReportSubscription
from apps.reports.services import REPORT_KEYS
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "rep-body"}
NAME = "Repairs & PMs <Q3>"


@pytest.fixture(autouse=True)
def _today(freeze_today):
    freeze_today(TODAY)


def toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


def sign_in(client, make_user, slug, email=True):
    user = make_user(slug)
    if email:
        user.email = user.username
        user.save(update_fields=["email"])
    client.force_login(user)
    return user


def custom_user(tenant, levels, slug="custom"):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    email=f"{slug}@riverside.example")


def form(**changes) -> dict:
    """The builder's form as the browser sends it: a repair cost report, grouped by department."""
    data = {"name": "Repair cost by department", "source": "work_orders", "col": ["number", "department", "total_cost"],
            "f_type": ["repair"], "date_field": "completed", "date_period": "", "date_from": "", "date_to": "",
            "group_by": "", "sort": "total_cost", "sort_dir": "desc"}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


@pytest.fixture
def saved(world):  # noqa: F811
    return custom.create_custom_report(name=NAME, source="work_orders", columns=["number", "device", "completed", "total_cost"],
                                       filters={"type": ["repair"]}, sort="-total_cost")


# --- the list and the button --------------------------------------------------------------------------------------------------

def test_the_list_shows_custom_reports_under_the_eight(client, make_user, world):  # noqa: F811
    zeta = custom.create_custom_report(name="zeta fleet", source="devices", columns=["tag"])
    alpha = custom.create_custom_report(name="Alpha labor", source="labor", columns=["hours"])
    sign_in(client, make_user, "technician")  # Reports View
    body = client.get("/reports/").content.decode()
    group = body.index('<div class="rep-grp" id="rep-custom" role="heading" aria-level="2">Custom reports</div>')
    assert body.index("Recall response log") < group < body.index("Alpha labor") < body.index("zeta fleet")  # by name, in any case
    for report in (alpha, zeta):
        url = f"/reports/{report.key}/"
        assert f'href="{url}" hx-get="{url}" hx-target="#rep-body" hx-swap="outerHTML" hx-push-url="true"' in body
    assert "<div class=\"s\" style=\"white-space:normal\">Labor (time logged)</div>" in body
    assert "/reports/custom/new/" not in body and "None yet" not in body  # Reports View does not build


@pytest.mark.parametrize("role, builds", [("director", True), ("analyst", True), ("manager", False), ("technician", False)])
def test_the_custom_report_button_is_for_reports_edit(client, make_user, ctx, role, builds):
    sign_in(client, make_user, role)
    body = client.get("/reports/").content.decode()
    button = ('<a class="btn" href="/reports/custom/new/" hx-get="/reports/custom/new/" hx-target="#rep-body" hx-swap="outerHTML" '
              'hx-push-url="true">')
    assert (button in body) is builds and (" Custom report</a>" in body) is builds
    assert ("None yet. Custom report builds one" in body) is builds  # an empty group says how to fill it
    assert "Report builder not built in this mock" not in body


# --- the builder ----------------------------------------------------------------------------------------------------------------

def test_the_builder_is_a_page_and_a_panel(client, make_user, world):  # noqa: F811
    sign_in(client, make_user, "analyst")
    page = client.get("/reports/custom/new/")
    body = page.content.decode()
    assert page.status_code == 200 and page.templates[0].name == "web/reports.html"
    assert "<title>New custom report · Reports · Riverside Regional</title>" in body and "<h2>New custom report</h2>" in body
    assert 'hx-post="/reports/custom/new/" hx-target="#rep-body" hx-swap="outerHTML" novalidate' in body and 'name="csrfmiddlewaretoken"' in body
    sources = re.findall(r'<option value="(\w+)"( selected)?>', body.split('id="cr-source"')[1].split("</select>")[0])
    assert sources == [("work_orders", " selected"), ("devices", ""), ("labor", ""), ("parts", "")]
    assert 'hx-get="/reports/custom/fields/" hx-target="#cr-fields" hx-swap="innerHTML" hx-trigger="change"' in body
    for key in custom.WORK_ORDERS.defaults:
        assert f'name="col" value="{key}" checked' in body
    assert 'name="col" value="labor_cost">' in body  # offered, not chosen
    assert f'name="f_department" value="{world["med"].pk}"' in body and ">Med-Surg 4</span>" in body
    assert 'name="f_technician" value="vendor"' not in body  # vendor time is a labor line's
    assert '<option value="completed_month">Completed (month)</option>' in body and '<option value="last_30">Last 30 days</option>' in body
    assert 'hx-post="/reports/custom/preview/" hx-target="#cr-preview" hx-swap="innerHTML">Preview</button>' in body
    panel = client.get("/reports/custom/new/", **BODY)
    assert panel.templates[0].name == "web/_reports_body.html" and "<html" not in panel.content.decode()
    assert panel.content.decode().lstrip().startswith("<title>New custom report · Reports · Riverside Regional</title>")
    assert client.get("/reports/custom/new/?source=parts").context["builder"]["typed"]["source"] == "parts"


def test_switching_the_source_brings_its_columns_and_filters(client, make_user, world):  # noqa: F811
    sign_in(client, make_user, "analyst")
    r = client.get("/reports/custom/fields/?source=labor", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r.templates[0].name == "web/_custom_report_fields.html"
    assert 'name="col" value="who" checked' in body and 'name="col" value="number"' not in body
    assert 'name="f_technician" value="vendor"' in body and ">Vendor time</span>" in body and ">Dana Whitfield</span>" in body
    assert '<div id="cr-preview" class="cr-preview" aria-live="polite" hx-swap-oob="true"></div>' in body  # the old preview goes
    assert '<option value="date">Date worked</option>' in body
    direct = client.get("/reports/custom/fields/?source=labor")
    assert direct.status_code == 302 and direct["Location"] == "/reports/custom/new/?source=labor"
    assert client.get("/reports/custom/fields/?source=<x>")["Location"] == "/reports/custom/new/"
    assert "Choose what the report lists." in client.get("/reports/custom/fields/?source=users", **HX).content.decode()


def test_preview_runs_the_form_and_saves_nothing(client, make_user, world, monkeypatch):  # noqa: F811
    sign_in(client, make_user, "analyst")
    r = client.post("/reports/custom/preview/", form(), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r.templates[0].name == "web/_custom_report_preview.html"
    assert "Work orders · Type: Corrective repair · Sorted by Total cost, highest first" in body
    assert '<th>Number</th><th>Department</th><th class="num">Total cost</th>' in body
    assert f'<td>{world["r2"].number}</td><td>Med-Surg 4</td><td class="num">$420.00</td>' in body
    assert '<td>Total</td><td class="muted">—</td><td class="num">$773.97</td>' in body  # every repair: 193.97 + 420 + 160
    assert "4 rows" in body and CustomReport.objects.count() == 0
    monkeypatch.setattr(custom, "PREVIEW_ROWS", 2)
    assert "Preview: the first 2 of 4 rows." in client.post("/reports/custom/preview/", form(), **HX).content.decode()
    grouped = client.post("/reports/custom/preview/", form(group_by="department", sort="count"), **HX).content.decode()
    assert "<th>Department</th><th class=\"num\">Count</th><th class=\"num\">Total cost</th>" in grouped
    assert "Left out of the grouped table: Number, Department." not in grouped and "Left out of the grouped table: Number." in grouped
    bad = client.post("/reports/custom/preview/", form(col=[], sort="", date_period="custom"), **HX).content.decode()
    assert "Fix these to preview: Choose at least one column. Enter a start date, an end date, or both." in bad
    assert client.get("/reports/custom/preview/")["Location"] == "/reports/custom/new/"


def test_saving_shows_the_new_report(client, make_user, world):  # noqa: F811
    kim = sign_in(client, make_user, "analyst")
    r = client.post("/reports/custom/new/", form(group_by="department"), **BODY)
    report = CustomReport.objects.get()
    assert (report.name, report.source, report.columns, report.filters, report.group_by, report.sort, report.created_by) == (
        "Repair cost by department", "work_orders", ["number", "department", "total_cost"], {"type": ["repair"]}, "department", "-total_cost", kim)
    assert r.status_code == 200 and r.templates[0].name == "web/_reports_body.html"
    assert r["HX-Push-Url"] == f"/reports/{report.key}/" and toast(r) == "Saved Repair cost by department"
    body = r.content.decode()
    assert "<h2>Repair cost by department</h2>" in body
    assert 'hx-push-url="true" aria-current="page"><div class="bd"><div class="t">Repair cost by department</div>' in body
    assert f'href="/print/reports/{report.key}/" target="_blank"' in body and f'href="/reports/{report.key}.csv" download' in body
    assert f'id="rep-schedule-{report.key}"' in body
    assert f'hx-get="/reports/custom-{report.pk}/edit/"' in body and f'hx-post="/reports/custom-{report.pk}/delete/"' in body
    assert report.history.get().history_user == kim
    plain = client.post("/reports/custom/new/", form(name="Without htmx"))
    assert plain.status_code == 302 and plain["Location"] == f"/reports/{CustomReport.objects.get(name='Without htmx').key}/"


def test_a_refused_save_keeps_what_was_typed_and_marks_each_part(client, make_user, world):  # noqa: F811
    sign_in(client, make_user, "analyst")
    custom.create_custom_report(name="Taken", source="devices", columns=["tag"])
    r = client.post("/reports/custom/new/", form(name="TAKEN", col=["number"], sort="total_cost", f_type=["repair", "pm"], date_period="custom",
                                                 date_from="2026-09-30", date_to="2026-09-01", group_by="vendor_service"), **BODY)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Push-Url" not in r and "HX-Trigger" not in r and CustomReport.objects.count() == 1
    assert "The report was not saved. Check the parts marked below." in body
    assert 'value="TAKEN"' in body and "Another custom report here is already called TAKEN." in body
    assert "The start date must be on or before the end date." in body
    assert 'name="col" value="number" checked' in body and 'name="col" value="department">' in body  # as typed, not the defaults
    assert 'name="f_type" value="pm" checked' in body and 'name="f_type" value="repair" checked' in body
    assert '<option value="vendor_service" selected>' in body and '<option value="custom" selected>' in body
    assert 'name="date_from" value="2026-09-30"' in body
    none = client.post("/reports/custom/new/", form(name="", col=[], sort=""), **BODY).content.decode()
    assert "Name the report." in none and "Choose at least one column." in none and 'aria-invalid="true"' in none


def test_editing_a_report(client, make_user, world, saved, other_tenant):  # noqa: F811
    sign_in(client, make_user, "director")
    page = client.get(f"/reports/custom-{saved.pk}/edit/")
    body = page.content.decode()
    assert page.status_code == 200 and "<h2>Edit Repairs &amp; PMs &lt;Q3&gt;</h2>" in body
    assert 'value="Repairs &amp; PMs &lt;Q3&gt;"' in body and 'name="col" value="completed" checked' in body
    assert 'name="f_type" value="repair" checked' in body and '<option value="total_cost" selected>' in body
    assert '<option value="desc" selected>' in body
    assert 'aria-current="page"><div class="bd"><div class="t">Repairs &amp; PMs &lt;Q3&gt;</div>' in body  # the list marks it
    r = client.post(f"/reports/custom-{saved.pk}/edit/", form(name="Repairs by month", col=["completed", "total_cost"], group_by="completed_month",
                                                               sort="completed_month", sort_dir="asc"), **BODY)
    saved.refresh_from_db()
    assert (saved.name, saved.group_by, saved.sort) == ("Repairs by month", "completed_month", "completed_month")
    assert toast(r) == "Saved Repairs by month" and r["HX-Push-Url"] == f"/reports/{saved.key}/"
    assert "<td>Sep 2025</td>" in r.content.decode()
    with tenant_context(other_tenant):
        theirs = custom.create_custom_report(name="Theirs", source="devices", columns=["tag"])
    assert client.get(f"/reports/custom-{theirs.pk}/edit/").status_code == 404
    assert client.post(f"/reports/custom-{theirs.pk}/delete/", **HX).status_code == 404
    assert client.get(f"/reports/{theirs.key}/").status_code == 404 and client.get(f"/reports/{theirs.key}.csv").status_code == 404
    assert client.get(f"/print/reports/{theirs.key}/").status_code == 404


def test_deleting_a_report_and_its_schedules(client, make_user, world, saved):  # noqa: F811
    nia = sign_in(client, make_user, "director")
    subs.set_subscription(nia, saved.key, "weekly")
    body = client.get(f"/reports/{saved.key}/").content.decode()
    assert ('hx-confirm="Delete the custom report Repairs &amp; PMs &lt;Q3&gt;? It is removed for everyone in the facility, and anyone who has '
            'it emailed stops getting it."') in body
    assert client.get(f"/reports/custom-{saved.pk}/delete/").status_code == 405
    r = client.post(f"/reports/custom-{saved.pk}/delete/", **BODY)
    assert r.status_code == 200 and toast(r) == f"Deleted {NAME} and 1 email schedule" and r["HX-Push-Url"] == "/reports/"
    assert "<h2>Cost of service ratio by category</h2>" in r.content.decode() and "Repairs &amp; PMs" not in r.content.decode()
    assert not CustomReport.objects.exists() and not ReportSubscription.objects.exists()
    assert client.get(f"/reports/{saved.key}/").status_code == 404
    again = custom.create_custom_report(name="Again", source="devices", columns=["tag"])
    plain = client.post(f"/reports/custom-{again.pk}/delete/")
    assert plain.status_code == 302 and plain["Location"] == "/reports/"


# --- a custom report's panel, CSV, print page, and schedule ---------------------------------------------------------------------

def test_a_custom_reports_panel(client, make_user, world, saved):  # noqa: F811
    sign_in(client, make_user, "technician")  # Reports View, Work orders View
    r = client.get(f"/reports/{saved.key}/")
    body = r.content.decode()
    assert r.status_code == 200 and r.context["report"]["custom"] and "web/_report_custom.html" in [t.name for t in r.templates]
    assert "<title>Repairs &amp; PMs &lt;Q3&gt; · Reports · Riverside Regional</title>" in body and "<script>" not in body.split("<body")[1]
    assert '<p class="cr-desc">Work orders · Type: Corrective repair · Sorted by Total cost, highest first</p>' in body
    assert '<th>Number</th><th>Device</th><th>Completed</th><th class="num">Total cost</th>' in body
    r1 = world["r1"]
    assert f'<td>{r1.number}</td><td>ICU ventilator</td><td>Sep 25, 2026</td><td class="num">$193.97</td>' in body
    assert f'<td>{world["r2"].number}</td><td>Infusion pump</td><td class="muted">—</td><td class="num">$420.00</td>' in body
    assert '<td class="num">$773.97</td></tr></tfoot>' in body and "4 rows" in body
    assert f'href="/print/reports/{saved.key}/"' in body and f'href="/reports/{saved.key}.csv" download' in body
    assert f'hx-get="/reports/custom-{saved.pk}/edit/"' not in body and "Delete</button>" not in body  # Reports View only
    swapped = client.get(f"/reports/{saved.key}/", **BODY)
    assert swapped.templates[0].name == "web/_reports_body.html"


def test_the_screen_shows_the_first_rows_and_the_csv_the_rest(client, make_user, world, saved, monkeypatch):  # noqa: F811
    sign_in(client, make_user, "analyst")
    monkeypatch.setattr(custom, "SCREEN_ROWS", 2)
    monkeypatch.setattr(custom, "MAX_ROWS", 3)
    body = client.get(f"/reports/{saved.key}/").content.decode()
    assert "Showing the first 2 of 4 rows; the CSV has the first 3." in body and body.count("<tr><td>WO-") == 2
    assert "The totals row covers every work order, not only those listed." in body
    rows = csv_rows(client.get(f"/reports/{saved.key}.csv"))
    assert rows[0] == ["Number", "Device", "Completed", "Total cost"] and len(rows) == 4
    monkeypatch.setattr(custom, "MAX_ROWS", 10)
    assert "Showing the first 2 of 4 rows; the CSV has all of them." in client.get(f"/reports/{saved.key}/").content.decode()


def test_its_csv_and_print_page(client, make_user, world, saved):  # noqa: F811
    sign_in(client, make_user, "analyst")
    r = client.get(f"/reports/{saved.key}.csv")
    assert r["Content-Disposition"] == 'attachment; filename="cadence-repairs-pms-q3-2026-10-02.csv"'
    assert csv_text(r).splitlines()[:2] == ["Number,Device,Completed,Total cost", f"{world['r2'].number},Infusion pump,,420.00"]
    p = client.get(f"/print/reports/{saved.key}/")
    body = p.content.decode()
    assert p.status_code == 200 and [t.name for t in p.templates[:2]] == ["web/print_report.html", "web/print_base.html"]
    assert "web/_report_custom.html" in [t.name for t in p.templates]
    assert "<h1>Repairs &amp; PMs &lt;Q3&gt;</h1>" in body and "<title>Repairs &amp; PMs &lt;Q3&gt; · Riverside Regional</title>" in body
    assert '<p class="rp-sub">Work orders · Type: Corrective repair · Sorted by Total cost, highest first</p>' in body
    assert "Riverside Regional · As of Oct 2, 2026" in body and '<p class="cr-desc">' not in body and "$193.97" in body
    dated = custom.create_custom_report(name="Last month", source="work_orders", columns=["number"], filters={"date": {"field": "completed",
                                        "period": "last_month"}})
    assert "Completed: last month (Sep 1, 2026 to Sep 30, 2026)" in client.get(f"/print/reports/{dated.key}/").content.decode()


def test_its_schedule_button(client, make_user, world, saved):  # noqa: F811
    kim = sign_in(client, make_user, "analyst")
    body = client.get(f"/reports/{saved.key}/").content.decode()
    assert f'id="rep-schedule-{saved.key}"' in body and f'hx-get="/reports/{saved.key}/schedule/" hx-target="#modal-card"' in body
    modal = client.get(f"/reports/{saved.key}/schedule/", **HX).content.decode()
    assert "<strong>Repairs &amp; PMs &lt;Q3&gt;</strong>" in modal and f'hx-post="/reports/{saved.key}/schedule/"' in modal
    r = client.post(f"/reports/{saved.key}/schedule/", {"frequency": "monthly"}, **HX)
    assert toast(r) == f"Scheduled: {NAME}, first Monday of each month to {kim.email}"
    assert f'id="rep-schedule-{saved.key}" hx-swap-oob="true"' in r.content.decode()
    assert subs.subscription_for(kim, saved.key).frequency == "monthly"


# --- who may ----------------------------------------------------------------------------------------------------------------------

def test_reports_view_runs_custom_reports_but_never_builds_them(client, make_user, world, saved):  # noqa: F811
    sign_in(client, make_user, "technician")
    assert client.get(f"/reports/{saved.key}/").status_code == 200 and client.get(f"/reports/{saved.key}.csv").status_code == 200
    for url in ("/reports/custom/new/", "/reports/custom/fields/?source=labor", f"/reports/custom-{saved.pk}/edit/"):
        assert client.get(url, **HX).status_code == 403, url
        assert client.get(url).status_code == 403, url
    for url in ("/reports/custom/new/", "/reports/custom/preview/", f"/reports/custom-{saved.pk}/edit/", f"/reports/custom-{saved.pk}/delete/"):
        assert client.post(url, form(), **HX).status_code == 403, url
    assert CustomReport.objects.count() == 1 and CustomReport.objects.get().name == NAME


@pytest.mark.parametrize("slug", ["vendor", "requester"])
def test_a_scoped_user_is_refused_everywhere(client, make_user, world, saved, slug):  # noqa: F811
    user = make_user(slug)
    user.company, user.department = "Acme Biomed", "ICU"
    user.role.set_levels({Module.REPORTS: Level.FULL})  # whatever their levels
    user.save()
    client.force_login(user)
    for url in (f"/reports/{saved.key}/", f"/reports/{saved.key}.csv", f"/print/reports/{saved.key}/", f"/reports/{saved.key}/schedule/",
                "/reports/custom/new/", f"/reports/custom-{saved.pk}/edit/"):
        assert client.get(url, **HX).status_code == 403, url
    assert client.post(f"/reports/custom-{saved.pk}/delete/", **HX).status_code == 403
    assert client.post("/reports/custom/new/", form(), **HX).status_code == 403 and CustomReport.objects.count() == 1


def test_without_view_on_what_it_lists_it_is_refused_in_plain_words(client, tenant, world, saved):  # noqa: F811
    """Reports Edit and Equipment View, no Work orders View: a report on devices works; one on work orders says why not, everywhere."""
    client.force_login(custom_user(tenant, {Module.REPORTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    message = "This report lists work orders, so it needs Work orders View, which your role does not have."
    body = client.get(f"/reports/{saved.key}/").content.decode()
    assert f'<div class="note warn" role="alert">{message}</div>' in body and "$193.97" not in body and world["r1"].number not in body
    assert f"/reports/{saved.key}.csv" not in body and f"/print/reports/{saved.key}/" not in body and "rep-schedule" not in body
    assert f'hx-post="/reports/custom-{saved.pk}/delete/"' in body  # deleting it shows nothing it lists
    csv = client.get(f"/reports/{saved.key}.csv")
    assert csv.status_code == 403 and csv.content.decode() == message and csv["Content-Type"].startswith("text/plain")
    printed = client.get(f"/print/reports/{saved.key}/")
    assert printed.status_code == 403 and printed.content.decode() == message
    modal = client.get(f"/reports/{saved.key}/schedule/", **HX).content.decode()
    assert message in modal and 'name="frequency"' not in modal
    preview = client.post("/reports/custom/preview/", form(), **HX).content.decode()
    assert "You need Work orders View to build a report that lists work orders." in preview and world["r1"].number not in preview
    builder = client.get("/reports/custom/new/").content.decode()
    assert '<option value="devices" selected>' in builder and 'value="work_orders"' not in builder
    refused = client.post("/reports/custom/new/", form(), **BODY).content.decode()
    assert "You need Work orders View to build a report that lists work orders." in refused and CustomReport.objects.count() == 1
    edit = client.get(f"/reports/custom-{saved.pk}/edit/").content.decode()
    assert "You need Work orders View to build a report that lists work orders." in edit and 'id="cr-form"' not in edit
    assert "Choose what the report lists." not in edit
    assert "You need Work orders View" in client.get("/reports/custom/fields/?source=labor", **HX).content.decode()
    r = client.post("/reports/custom/new/", form(name="Fleet", source="devices", col=["tag", "status"], f_type=None, sort="", date_field="installed"),
                    **BODY)
    assert toast(r) == "Saved Fleet" and "CE-10001" in r.content.decode()


def test_names_are_escaped_everywhere(client, make_user, world):  # noqa: F811
    sign_in(client, make_user, "analyst")
    evil = '<img src=x onerror=alert(1)>"\''
    r = client.post("/reports/custom/new/", form(name=evil), **BODY)
    report = CustomReport.objects.get()
    assert report.name == evil
    for body in (r.content.decode(), client.get(f"/reports/{report.key}/").content.decode(), client.get(f"/print/reports/{report.key}/").content.decode(),
                 client.get(f"/reports/custom-{report.pk}/edit/").content.decode(), client.get(f"/reports/{report.key}/schedule/", **HX).content.decode()):
        assert "<img src=x" not in body and "&lt;img src=x onerror=alert(1)&gt;" in body


def test_the_api_keeps_serving_the_eight(client, make_user, saved):
    sign_in(client, make_user, "analyst")
    assert [r["key"] for r in client.get("/api/v1/reports/").json()] == REPORT_KEYS
    assert client.get(f"/api/v1/reports/{saved.key}/").status_code == 404


# --- PostgreSQL row-level security ----------------------------------------------------------------------------------------------

@needs_postgres
def test_each_source_saves_and_runs_under_the_policies(client, make_user, world):  # noqa: F811
    """As the runtime role: build a report of each source through the screen, then open it, download it, and print it."""
    sign_in(client, make_user, "director")
    as_app_role()
    builds = {"work_orders": (["number", "total_cost", "days_open"], world["r1"].number), "devices": (["tag", "service_cost_12mo", "age"], "CE-10001"),
              "labor": (["who", "amount", "tag"], "Dana Whitfield"), "parts": (["description", "amount", "tag"], "Flow sensor")}
    for source, (columns, expect) in builds.items():
        r = client.post("/reports/custom/new/", form(name=f"Under RLS {source}", source=source, col=columns, f_type=None, sort=columns[1],
                                                     date_field=custom.SOURCES[source].dates[0]), **BODY)
        assert r.status_code == 200 and toast(r) == f"Saved Under RLS {source}", r.content.decode()[:2000]
        key = r["HX-Push-Url"].strip("/").split("/")[-1]
        assert expect in client.get(f"/reports/{key}/").content.decode()
        assert expect in csv_text(client.get(f"/reports/{key}.csv"))
        assert expect in client.get(f"/print/reports/{key}/").content.decode()
        grouped = client.post("/reports/custom/preview/", form(source=source, col=columns, f_type=None, sort="count", sort_dir="desc",
                                                               group_by=custom.group_options(custom.SOURCES[source])[0][0]), **HX)
        assert grouped.status_code == 200 and "Count</th>" in grouped.content.decode()
    with tenant_context(world["vent"].tenant):
        assert CustomReport.objects.count() == 4
