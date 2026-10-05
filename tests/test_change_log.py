"""
The change log (slice 20, part B): the Users and access tab's Change log (/users/log/), its CSV and printable page, and
GET /api/v1/change-log/. The facility's changes newest first, across the areas the reader's role can view (apps.core.history), the
access events apps.accounts.services writes among them; Users View, and never a scoped user's whatever their levels; filters in the
address, a page of 50 with Show older; another facility's changes never; and the same as the runtime role under row-level security.
"""
from datetime import date, timedelta

import pytest
from csvutil import csv_rows, csv_text
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token
from simple_history.utils import update_change_reason

from apps.accounts import services
from apps.accounts.models import AccessEvent, DataScope, Level, Module, Role, User, create_default_roles
from apps.api.tenancy import NO_TENANT
from apps.contracts import services as ct
from apps.contracts.models import Contract
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.web import views_change_log

LOG, CSV, PRINT, API = "/users/log/", "/users/log/export.csv", "/users/log/print/", "/api/v1/change-log/"
HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
ENTRY_FIELDS = {"at", "who", "who_id", "area", "area_label", "record", "url", "action", "action_label", "reason", "changes"}


def hx(target):
    return {**HX, "HTTP_HX_TARGET": target}


def role(slug):
    return Role.objects.get(slug=slug)


@pytest.fixture
def kim(ctx, make_user):
    return make_user("director", username="kim@riverside.example")


@pytest.fixture
def world(ctx, kim, make_user, dept, vent_model):
    """A device added and moved (with a reason), a contract added by Kim and renewed by Tom, and Tom's role changed by Kim."""
    tom = make_user("technician", username="tom@riverside.example")
    asset = eq.create_asset(tag="CE-10001", device_model=vent_model, department=dept, room="12", by=kim)
    eq.update_asset(asset, room="14", by=kim)
    update_change_reason(Asset.objects.get(pk=asset.pk), "Moved for the renovation")
    contract = ct.create_contract(reference="SC-2291", vendor="Philips", start_on=TODAY - timedelta(days=30), end_on=TODAY + timedelta(days=300),
                                  annual_cost=1200, by=kim)
    ct.update_contract(contract, annual_cost=1500, by=tom)
    services.set_role_level(role("analyst"), "pm", Level.EDIT, by=kim)
    return {"tom": tom, "asset": asset, "contract": contract}


@pytest.fixture
def auditor(ctx, make_user):
    """A custom role that reads Users and Equipment and nothing else: its log has devices, device models, credentials, roles, and
    access, never contracts or work orders."""
    r = Role.objects.create(name="Equipment auditor", slug="equipment-auditor")
    r.set_levels({Module.USERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    return User.objects.create_user(username="audit@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=r, first_name="Ada",
                                    last_name="Auditor")


def shift(model, days, **filters):
    """Move `model`'s history rows matching `filters` `days` back, as if they were made then."""
    model.history.filter(**filters).update(history_date=timezone.now() - timedelta(days=days))


# --- the tab --------------------------------------------------------------------------------------------------------------------

def test_the_tab_lists_changes_newest_first_with_who_what_and_why(client, kim, world):
    client.force_login(kim)
    r = client.get(LOG)
    assert r.status_code == 200 and r.context["users_tab"] == "log"
    entries = r.context["entries"]
    assert [e.at for e in entries] == sorted((e.at for e in entries), reverse=True)
    assert entries[0].area == "access" and entries[0].changes[0].after == "PM schedule: View → Edit"
    html = r.content.decode()
    assert 'aria-current="page">Change log</a>' in html
    moved = next(e for e in entries if e.area == "devices" and e.action == "changed")
    assert {c.field: (c.before, c.after) for c in moved.changes}["Room"] == ("12", "14") and moved.reason == "Moved for the renovation"
    assert '<span class="clog-old">12</span>' in html and '<span class="clog-new">14</span>' in html and "Moved for the renovation" in html
    # a device opens in the drawer over the log; a role opens its own page
    assert 'href="/equipment/CE-10001/" hx-get="/equipment/CE-10001/" hx-target="#drawer"' in html
    renewed = next(e for e in entries if e.area == "contracts" and e.action == "changed")
    assert renewed.who == "Technician User" and renewed.who_id == world["tom"].pk
    assert {c.field: (c.before, c.after) for c in renewed.changes}["Annual cost"] == ("$1,200", "$1,500")
    assert "Access · Role access changed" in html
    # Export and Print take the filters on screen
    assert f'data-base="{CSV}"' in html and f'data-base="{PRINT}"' in html and 'hx-push-url="true"' in html


def test_users_view_reads_it_and_nobody_else(client, ctx, make_user, world):
    manager = make_user("manager")  # Users View
    client.force_login(manager)
    assert client.get(LOG).status_code == 200 and client.get(CSV).status_code == 200 and client.get(PRINT).status_code == 200
    assert f'href="{LOG}"' in client.get("/users/").content.decode()
    for slug in ("technician", "analyst"):  # Users None
        client.force_login(make_user(slug))
        assert client.get(LOG).status_code == 403 and client.get(CSV).status_code == 403 and client.get(API).status_code == 403


@pytest.mark.parametrize("who", ["vendor", "requester", "full-company", "full-department"])
def test_a_scoped_user_never_reads_it_whatever_their_levels(client, ctx, make_user, world, who):
    if who in ("vendor", "requester"):
        user = make_user(who)
        user.company, user.department = "Philips", "ICU"
        user.save()
    else:
        scope = DataScope.COMPANY if who == "full-company" else DataScope.DEPARTMENT
        r = Role.objects.create(name=f"Scoped {scope}", slug=f"scoped-{scope}", scope=scope)
        r.set_levels({m: Level.FULL for m in Module.values})
        user = User.objects.create_user(username=f"{who}@riverside.example", password="x", tenant=ctx, role=r, company="Philips", department="ICU")
    client.force_login(user)
    for url in (LOG, CSV, PRINT, LOG + "?area=devices"):
        assert client.get(url).status_code == 403, url
        assert client.get(url, **hx("log-body")).status_code == 403, url
    r = client.get(API)
    assert r.status_code == 403
    if who.startswith("full"):  # Users Full, and still refused: the log is not narrowed to their share
        assert "part of this facility" in r.json()["detail"]


def test_a_role_sees_only_the_areas_it_can_view(client, auditor, world):
    client.force_login(auditor)
    r = client.get(LOG)
    seen = {"devices", "device_models", "roles", "access"}  # the device, its model, the facility's roles (added with it), Kim's change
    assert {e.area for e in r.context["entries"]} == seen
    assert [k for k, _ in r.context["areas"]] == ["devices", "device_models", "credentials", "roles", "access"]
    html = r.content.decode()
    assert 'value="contracts"' not in html and "SC-2291" not in html
    dropped = client.get(LOG + "?area=contracts")  # an area the role cannot view is dropped, as the lists drop a filter
    assert dropped.context["f"].area == "" and "SC-2291" not in dropped.content.decode()
    assert "SC-2291" not in csv_text(client.get(CSV + "?area=contracts")) and "SC-2291" not in client.get(PRINT).content.decode()
    api = client.get(API + "?area=contracts")
    assert api.status_code == 400 and api.json() == {"area": ["Your role cannot view Contracts."]}
    assert {e["area"] for e in client.get(API).json()["results"]} == seen


def test_filters_by_area_dates_and_who(client, kim, world):
    client.force_login(kim)
    tom = world["tom"]
    devices = client.get(LOG + "?area=devices").context["entries"]
    assert devices and {e.area for e in devices} == {"devices"}
    assert [e.record for e in client.get(LOG + f"?who={tom.pk}").context["entries"]] == ["SC-2291 · Philips"]
    shift(Asset, 10)
    week_ago = (TODAY - timedelta(days=7)).isoformat()
    recent = client.get(LOG + f"?from={week_ago}").context["entries"]
    assert recent and "devices" not in {e.area for e in recent}
    older = client.get(LOG + f"?to={week_ago}").context["entries"]
    assert older and {e.area for e in older} == {"devices"}
    assert client.get(LOG + f"?from={TODAY + timedelta(days=1)}").context["entries"] == []
    assert "No changes match these filters." in client.get(LOG + f"?from={TODAY + timedelta(days=1)}").content.decode()
    # what cannot be used is dropped, never an error
    junk = client.get(LOG + "?area=bogus&from=2026-13-01&to=yesterday&who=abc&page=x")
    assert junk.status_code == 200 and junk.context["f"] == views_change_log.LogFilters()
    # the form swaps the list alone
    body = client.get(LOG + "?area=devices", **hx("log-body"))
    html = body.content.decode()
    assert 'id="log-body"' in html and "<html" not in html and "Room" in html and "SC-2291" not in html


def test_who_is_a_person_in_this_facility(client, kim, world, other_tenant, make_user):
    create_default_roles(other_tenant)
    stranger = make_user("director", tenant_=other_tenant, username="dir@other.example")
    client.force_login(kim)
    r = client.get(LOG + f"?who={stranger.pk}")
    assert r.context["f"].who is None and r.context["entries"]  # dropped: the whole log
    assert stranger.pk not in [pk for pk, _ in r.context["people"]] and kim.pk in [pk for pk, _ in r.context["people"]]
    assert client.get(API + f"?who={stranger.pk}").status_code == 400


def test_another_facilitys_changes_never_show(client, kim, world, other_tenant, make_user):
    create_default_roles(other_tenant)
    theirs = make_user("director", tenant_=other_tenant, username="dir@other.example")
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="ED"))
        services.set_role_level(Role.objects.get(slug="technician"), "contracts", Level.EDIT, by=theirs)
    client.force_login(kim)
    for body in (client.get(LOG).content.decode(), csv_text(client.get(CSV)), client.get(PRINT).content.decode(), client.get(API).content.decode()):
        assert "THEIRS-1" not in body and "B650" not in body and "Contracts: View → Edit" not in body
    assert "PM schedule: View → Edit" in client.get(LOG).content.decode()


# --- paging ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def many(ctx, kim):
    """60 access events in the last hour, "Change 59" the newest; the roles' own history (added with the facility) a day before."""
    for n in range(60):
        AccessEvent.objects.create(action=AccessEvent.Action.ROLE_LEVEL_CHANGED, by=kim, role=role("analyst"), detail=f"Change {n:02d}")
    stamps = timezone.now() - timedelta(hours=1)
    for n, e in enumerate(AccessEvent.objects.filter(detail__startswith="Change").order_by("detail")):
        AccessEvent.objects.filter(pk=e.pk).update(at=stamps + timedelta(seconds=n))
    shift(Role, 1)


def test_a_page_of_50_then_show_older_adds_the_rest(client, kim, many):
    client.force_login(kim)
    r = client.get(LOG)
    assert len(r.context["entries"]) == 50 and r.context["more"] and r.context["entries"][0].changes[0].after == "Change 59"
    html = r.content.decode()
    assert 'id="log-more"' in html and 'hx-get="/users/log/?page=2" hx-target="#log-more" hx-swap="outerHTML"' in html
    older = client.get(LOG + "?area=access&page=2", **hx("log-more"))
    rows = older.content.decode()
    assert older.status_code == 200 and "<table" not in rows and rows.count("<tr>") == 10 and "Change 09" in rows and "Change 00" in rows
    assert 'id="log-more"' not in rows and "Change 10" not in rows
    # the filters ride along
    filtered = client.get(LOG + "?area=access").content.decode()
    assert 'hx-get="/users/log/?area=access&amp;page=2"' in filtered
    # without JavaScript, the next page opens on its own
    page2 = client.get(LOG + "?area=access&page=2")
    assert len(page2.context["entries"]) == 10 and "Back to the newest" in page2.content.decode()


def test_the_log_reaches_back_so_far_then_asks_for_dates(client, kim, many, monkeypatch):
    monkeypatch.setattr(views_change_log, "MAX_PAGE", 1)
    client.force_login(kim)
    r = client.get(LOG + "?page=5")
    assert r.context["f"].page == 1 and not r.context["more"] and r.context["at_limit"]
    html = r.content.decode()
    assert 'id="log-more"' not in html and "narrow the dates to see older ones" in html


# --- CSV and print --------------------------------------------------------------------------------------------------------------

def test_the_csv_has_the_filters_on_screen_newest_first(client, kim, world, monkeypatch):
    client.force_login(kim)
    r = client.get(CSV + "?area=devices")
    assert r["Content-Disposition"] == f'attachment; filename="cadence-change-log-{timezone.localdate():%Y-%m-%d}.csv"'
    rows = csv_rows(r)
    assert rows[0] == ["When", "Who", "Area", "Record", "Action", "What changed", "Reason"]
    assert {row[2] for row in rows[1:]} == {"Devices"} and rows[1][4] == "Changed" and "Room: 12 → 14" in rows[1][5]
    assert rows[1][6] == "Moved for the renovation" and rows[-1][4] == "Added"
    everything = csv_rows(client.get(CSV))
    access = next(row for row in everything if row[2] == "Access")
    assert access[3] == "Finance and quality" and access[4] == "Role access changed" and access[5] == "PM schedule: View → Edit"
    monkeypatch.setattr(views_change_log, "CSV_LIMIT", 2)
    assert len(csv_rows(client.get(CSV))) == 3


def test_the_csv_keeps_formula_looking_text_as_text(client, kim, world):
    update_change_reason(Contract.objects.get(pk=world["contract"].pk), "=HYPERLINK(\"http://x\")")
    client.force_login(kim)
    reasons = [row[6] for row in csv_rows(client.get(CSV + "?area=contracts"))]
    assert "'=HYPERLINK(\"http://x\")" in reasons


def test_the_printable_page(client, kim, world, monkeypatch):
    client.force_login(kim)
    week_ago = TODAY - timedelta(days=7)
    html = client.get(PRINT + f"?area=devices&from={week_ago.isoformat()}&who={kim.pk}").content.decode()
    assert f"Devices · from {week_ago:%b} {week_ago.day}, {week_ago.year} · by Director User" in html
    assert "<title>Change log · Riverside Regional</title>" in html
    html = client.get(PRINT + "?area=devices").content.decode()
    assert "Room: 12 → 14" in html and "Moved for the renovation" in html and "SC-2291" not in html
    monkeypatch.setattr(views_change_log, "PRINT_LIMIT", 1)
    assert "the latest 1 changes" in client.get(PRINT).content.decode()


# --- the API --------------------------------------------------------------------------------------------------------------------

def test_the_api_lists_entries_as_json(client, kim, world):
    client.force_login(kim)
    data = client.get(API).json()
    assert set(data) == {"results", "more", "offset", "limit", "next"} and data["more"] is False and data["next"] is None
    assert all(set(e) == ENTRY_FIELDS for e in data["results"])
    first = data["results"][0]
    assert first["area"] == "access" and first["action"] == "role_level_changed" and first["action_label"] == "Role access changed"
    assert first["record"] == "Finance and quality" and first["who_id"] == kim.pk and first["url"] is None
    assert first["changes"] == [{"field": "", "before": "", "after": "PM schedule: View → Edit"}]
    moved = next(e for e in data["results"] if e["area"] == "devices" and e["action"] == "changed")
    assert moved["url"] == "http://testserver/equipment/CE-10001/" and moved["reason"] == "Moved for the renovation"
    assert {"field": "Room", "before": "12", "after": "14"} in moved["changes"]
    assert moved["at"].startswith(TODAY.isoformat()[:4])


def test_the_api_filters_and_pages(client, kim, world, many):
    client.force_login(kim)
    assert {e["area"] for e in client.get(API + "?area=devices").json()["results"]} == {"devices"}
    assert [e["record"] for e in client.get(API + f"?who={world['tom'].pk}").json()["results"]] == ["SC-2291 · Philips"]
    shift(Asset, 10)
    week_ago = (TODAY - timedelta(days=7)).isoformat()
    assert {e["area"] for e in client.get(API + f"?until={week_ago}").json()["results"]} == {"devices"}
    assert "devices" not in {e["area"] for e in client.get(API + f"?since={week_ago}&until={TODAY}").json()["results"]}
    page = client.get(API + "?area=access&limit=40").json()
    assert len(page["results"]) == 40 and page["more"] and page["next"] == "http://testserver/api/v1/change-log/?area=access&limit=40&offset=40"
    rest = client.get(page["next"]).json()
    assert len(rest["results"]) == 21 and not rest["more"] and rest["next"] is None and rest["offset"] == 40
    assert 61 + 4 < len(client.get(API + "?limit=200").json()["results"]) <= 200


@pytest.mark.parametrize("query, field", [
    ("area=bogus", "area"), ("since=2026-13-01", "since"), ("until=10/05/2026", "until"), ("since=2026-10-05&until=2026-10-01", "until"),
    ("who=abc", "who"), ("who=999999", "who"), ("limit=0", "limit"), ("limit=201", "limit"), ("offset=-1", "offset"), ("offset=10001", "offset"),
    ("limit=ten", "limit"),
])
def test_the_api_refuses_a_bad_parameter_by_name(client, kim, query, field):
    client.force_login(kim)
    r = client.get(f"{API}?{query}")
    assert r.status_code == 400 and list(r.json()) == [field], (query, r.json())


def test_the_api_is_read_only_and_needs_a_facility(client, kim, world, db):
    token = Token.objects.create(user=kim)
    r = client.get(API, HTTP_AUTHORIZATION=f"Token {token.key}")
    assert r.status_code == 200 and r.json()["results"]
    assert client.post(API, {}, content_type="application/json", HTTP_AUTHORIZATION=f"Token {token.key}").status_code == 405
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    r = client.get(API, HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=root).key}")
    assert r.status_code == 403 and r.json() == {"detail": NO_TENANT}


# --- under row-level security ---------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_change_log_as_the_runtime_role(client, kim, world, other_tenant, make_user):
    """As the runtime role: a role change made on the Users tab writes its access event inside the facility, and the tab, the
    Show older rows, the CSV, the printable page, and the API read the facility's history and events, and nothing of another's."""
    create_default_roles(other_tenant)
    theirs = make_user("director", tenant_=other_tenant, username="dir@other.example")
    with tenant_context(other_tenant):
        services.set_role_level(Role.objects.get(slug="technician"), "contracts", Level.EDIT, by=theirs)
    tom = world["tom"]
    client.force_login(kim)
    as_app_role()
    r = client.post(f"/users/{tom.pk}/role/", {"role": str(role("manager").id)}, **HX)
    assert r.status_code == 200
    page = client.get(LOG)
    assert page.status_code == 200
    html = page.content.decode()
    assert "Technician → CE manager" in html and "Room" in html and "SC-2291" in html and "Contracts: View → Edit" not in html
    assert client.get(LOG + "?page=2", **hx("log-more")).status_code == 200
    assert "Technician → CE manager" in csv_text(client.get(CSV)) and "CE-10001" in client.get(PRINT).content.decode()
    data = client.get(API).json()
    assert data["results"][0]["changes"][0]["after"] == "Technician → CE manager"
    assert "Contracts: View → Edit" not in str(data)
    with tenant_context(kim.tenant):
        assert AccessEvent.objects.filter(action=AccessEvent.Action.ROLE_CHANGED, by=kim, user=tom).count() == 1
