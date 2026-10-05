"""
The change log (slice 20, part B): the Users and access tab's Change log (/users/log/), its CSV and printable page, and
GET /api/v1/change-log/. The facility's changes newest first, across the areas the reader's role can view (apps.core.history), the
access events apps.accounts.services writes among them; Users View, and never a scoped user's whatever their levels; filters in the
address, a page of 50 with Show older (a cursor: the last entry shown, so changes saved meanwhile never repeat or skip one); another
facility's changes never, nor the name of anyone of another facility; a device model's link only for a reader who may open it (PM
View); and the same as the runtime role under row-level security.
"""
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from csvutil import csv_rows, csv_text
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token
from simple_history.utils import update_change_reason

from apps.accounts import services
from apps.accounts.models import AccessEvent, DataScope, Level, Module, Role, User, create_default_roles
from apps.api.tenancy import NO_TENANT
from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType
from apps.core import history
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
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
    junk = client.get(LOG + "?area=bogus&from=2026-13-01&to=yesterday&who=abc&after=x&page=2")
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


def test_someone_of_another_facility_is_never_named(client, kim, world, other_tenant, make_user):
    """A change recorded for a user of another facility (the public form, before it recorded nobody) reads as "Someone outside this
    facility", with no id, on the tab, the CSV, the printable page, and the API."""
    create_default_roles(other_tenant)
    stranger = make_user("director", tenant_=other_tenant, username="sandra@other.example")
    stranger.first_name, stranger.last_name = "Sandra", "Stranger"
    stranger.save()
    Asset.history.filter(id=world["asset"].pk, room="14").update(history_user=stranger)
    AccessEvent.objects.create(action=AccessEvent.Action.ROLE_CHANGED, by=stranger, user=world["tom"], role=role("manager"), detail="Technician → CE manager")
    client.force_login(kim)
    r = client.get(LOG)
    outside = [e for e in r.context["entries"] if e.who == history.OUTSIDE]
    assert {e.area for e in outside} == {"devices", "access"} and all(e.who_id is None for e in outside)
    html = r.content.decode()
    assert '<td class="clog-who">Someone outside this facility</td>' in html and "Stranger" not in html and "Sandra" not in html
    assert stranger.pk not in [pk for pk, _ in r.context["people"]]
    data = client.get(API).json()["results"]
    theirs = [e for e in data if e["who"] == history.OUTSIDE]
    assert {e["area"] for e in theirs} == {"devices", "access"} and all(e["who_id"] is None for e in theirs)
    assert "Stranger" not in str(data) and "Sandra" not in str(data)
    assert "Stranger" not in csv_text(client.get(CSV)) and "Stranger" not in client.get(PRINT).content.decode()
    assert any(e["who"] == "Technician User" and e["who_id"] == world["tom"].pk for e in data)  # this facility's people by name


def test_a_device_model_links_only_for_a_reader_with_pm_view(client, kim, auditor, world, vent_model):
    """A device model opens in the PM program's drawer (PM View): a reader with Equipment View alone gets its entries without the
    link, on the tab and in the API; a director gets the link."""
    url = f"/pm/models/{vent_model.pk}/"
    client.force_login(auditor)  # Users View and Equipment View, no PM View
    assert client.get(url, **hx("drawer")).status_code == 403  # where the link would go
    r = client.get(LOG + "?area=device_models")
    models = r.context["entries"]
    assert models and all(e.area == "device_models" and e.url is None for e in models)
    html = r.content.decode()
    assert "Hamilton Medical Hamilton-G5" in html and url not in html
    api = client.get(API + "?area=device_models").json()["results"]
    assert api and all(e["url"] is None for e in api)
    assert all(e.url == "/equipment/CE-10001/" for e in client.get(LOG + "?area=devices").context["entries"])  # links they may follow stay
    client.force_login(kim)  # PM View: the model opens in its drawer over the log
    r = client.get(LOG + "?area=device_models")
    assert r.context["entries"] and all(e.url == url for e in r.context["entries"]) and f'href="{url}"' in r.content.decode()
    assert {e["url"] for e in client.get(API + "?area=device_models").json()["results"]} == {f"http://testserver{url}"}


# --- paging ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def many(ctx, kim):
    """60 access events in the last hour, "Change 59" the newest; the roles' own history and the facility's (both added with the
    facility; the facility's is read since slice 21) a day before."""
    for n in range(60):
        AccessEvent.objects.create(action=AccessEvent.Action.ROLE_LEVEL_CHANGED, by=kim, role=role("analyst"), detail=f"Change {n:02d}")
    stamps = timezone.now() - timedelta(hours=1)
    for n, e in enumerate(AccessEvent.objects.filter(detail__startswith="Change").order_by("detail")):
        AccessEvent.objects.filter(pk=e.pk).update(at=stamps + timedelta(seconds=n))
    shift(Role, 1)
    shift(Tenant, 1, id=ctx.id)


def after_of(url: str) -> str:
    """The cursor a Show older link or the API's next carries (?after=)."""
    return parse_qs(urlparse(url).query)["after"][0]


def test_a_page_of_50_then_show_older_adds_the_rest(client, kim, many):
    client.force_login(kim)
    r = client.get(LOG)
    assert len(r.context["entries"]) == 50 and r.context["more"] and r.context["entries"][0].changes[0].after == "Change 59"
    cursor = r.context["next_after"]
    assert history.parse_cursor(cursor)[0] == r.context["entries"][-1].at  # the last entry shown
    html = r.content.decode()
    assert 'id="log-more"' in html and f'hx-get="{escape("/users/log/?" + urlencode({"after": cursor}))}" hx-target="#log-more" hx-swap="outerHTML"' in html
    assert "Back to the newest" not in html
    # the filters ride along
    filtered = client.get(LOG + "?area=access")
    access_cursor = filtered.context["next_after"]
    assert f'hx-get="{escape("/users/log/?" + urlencode({"area": "access", "after": access_cursor}))}"' in filtered.content.decode()
    older = client.get(LOG + "?" + urlencode({"area": "access", "after": access_cursor}), **hx("log-more"))
    rows = older.content.decode()
    assert older.status_code == 200 and "<table" not in rows and rows.count("<tr>") == 10 and "Change 09" in rows and "Change 00" in rows
    assert 'id="log-more"' not in rows and "Change 10" not in rows
    # without JavaScript, the next page opens on its own
    page2 = client.get(LOG + "?" + urlencode({"area": "access", "after": access_cursor}))
    html2 = page2.content.decode()
    assert len(page2.context["entries"]) == 10 and page2.context["f"].after == access_cursor and "Back to the newest" in html2
    assert 'href="/users/log/?area=access">Back to the newest</a>' in html2 and "No changes" not in html2


def test_show_older_reaches_any_depth_and_a_bad_cursor_reads_the_newest(client, kim, many, monkeypatch):
    """No cap on how far back Show older goes: each page continues after the last entry shown, every entry once."""
    monkeypatch.setattr(views_change_log, "PAGE", 7)
    client.force_login(kim)
    seen, after, pages = [], None, 0
    while True:
        r = client.get(LOG + "?" + urlencode({"area": "access", **({"after": after} if after else {})}))
        seen += [e.changes[0].after for e in r.context["entries"]]
        pages += 1
        if not r.context["more"]:
            assert 'id="log-more"' not in r.content.decode()
            break
        after = r.context["next_after"]
    assert pages == 9 and seen == [f"Change {n:02d}" for n in range(59, -1, -1)]  # 60 access events, 7 to a page
    for bad in ("nonsense", "2", "2026-10-05T12:00:00~11~1"):
        r = client.get(LOG + "?" + urlencode({"area": "access", "after": bad}))
        assert r.status_code == 200 and r.context["f"].after is None and r.context["entries"][0].changes[0].after == "Change 59"
        assert "Back to the newest" not in r.content.decode()


def test_a_save_between_two_pages_repeats_and_skips_nothing(client, kim, many, monkeypatch):
    monkeypatch.setattr(views_change_log, "PAGE", 5)
    client.force_login(kim)
    first = client.get(LOG + "?area=access")
    assert [e.changes[0].after for e in first.context["entries"]] == [f"Change {n}" for n in range(59, 54, -1)]
    services.set_role_level(role("analyst"), "pm", Level.EDIT, by=kim)  # a new access change while page 1 is on screen
    older = client.get(LOG + "?" + urlencode({"area": "access", "after": first.context["next_after"]}), **hx("log-more"))
    assert [e.changes[0].after for e in older.context["entries"]] == [f"Change {n}" for n in range(54, 49, -1)]


def test_saves_that_show_nothing_never_leave_an_empty_page_that_says_there_is_nothing(client, kim, dept, vent_model, monkeypatch):
    """A contract's type change re-saves each device it covers for its support type, which the log never shows. A page reads past
    those saves to fill itself; past MAX_ROUNDS windows it can come back empty, and then it says Show older, never "No changes"."""
    contract = ct.create_contract(reference="SC-9", vendor="Philips", type=ContractType.OEM, start_on=TODAY - timedelta(days=30),
                                  end_on=TODAY + timedelta(days=300), annual_cost=Decimal("1200"), by=kim)
    fleet = [Asset.objects.create(tag=f"CE-3{n:03d}", device_model=vent_model, department=dept) for n in range(6)]
    for asset in fleet:
        ct.add_asset(contract, asset)  # shown: each device's contract
    ct.update_contract(contract, type=ContractType.THIRD_PARTY, by=kim)  # 6 saves of nothing shown, the newest in the devices area
    client.force_login(kim)
    monkeypatch.setattr(views_change_log, "PAGE", 3)
    full = client.get(LOG + "?area=devices")  # 2 windows of nothing shown, then the devices' contract
    assert len(full.context["entries"]) == 3 and full.context["more"] and all(e.changes[0].field == "Contract" for e in full.context["entries"])
    monkeypatch.setattr(history, "MAX_ROUNDS", 1)  # one window of 3 saves, all of nothing shown
    empty = client.get(LOG + "?area=devices")
    html = empty.content.decode()
    assert empty.context["entries"] == [] and empty.context["more"] and 'id="log-more"' in html
    assert "No changes recorded yet." not in html and "No changes match these filters." not in html
    nxt = client.get(LOG + "?" + urlencode({"area": "devices", "after": empty.context["next_after"]}), **hx("log-more"))
    assert nxt.context["entries"] == [] and nxt.context["more"]  # the other 3 saves of nothing shown
    nxt = client.get(LOG + "?" + urlencode({"area": "devices", "after": nxt.context["next_after"]}), **hx("log-more"))
    assert [e.changes[0].field for e in nxt.context["entries"]] == ["Contract"] * 3
    data = client.get(API + "?area=devices&limit=3").json()  # the API says the same: more, with where to go on
    assert data["results"] == [] and data["more"] and after_of(data["next"])


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
    assert set(data) == {"results", "more", "limit", "after", "next"} and data["more"] is False and data["next"] is None and data["after"] is None
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
    cursor = after_of(page["next"])
    assert len(page["results"]) == 40 and page["more"] and page["after"] is None and history.parse_cursor(cursor) is not None
    assert page["next"] == "http://testserver/api/v1/change-log/?" + urlencode({"area": "access", "limit": "40", "after": cursor})
    rest = client.get(page["next"]).json()
    assert len(rest["results"]) == 21 and not rest["more"] and rest["next"] is None and rest["after"] == cursor
    assert 61 + 4 < len(client.get(API + "?limit=200").json()["results"]) <= 200
    assert client.get(API + "?area=access&limit=40&offset=40").json()["results"] == page["results"]  # offset is no parameter now: ignored


def test_the_apis_next_after_a_new_change_repeats_nothing(client, kim, world, many):
    client.force_login(kim)
    everything = client.get(API + "?area=access&limit=200").json()["results"]
    page = client.get(API + "?area=access&limit=10").json()
    assert page["results"] == everything[:10]
    services.set_role_level(role("analyst"), "contracts", Level.EDIT, by=kim)  # saved while the client pages
    rest = client.get(page["next"]).json()
    assert rest["results"] == everything[10:20] and rest["after"] == after_of(page["next"])
    assert after_of(rest["next"]) != after_of(page["next"])
    newest = client.get(API + "?area=access&limit=1").json()["results"][0]
    assert newest["changes"][0]["after"] == "Contracts: View → Edit" and newest not in rest["results"]


@pytest.mark.parametrize("query, field", [
    ("area=bogus", "area"), ("since=2026-13-01", "since"), ("until=10/05/2026", "until"), ("since=2026-10-05&until=2026-10-01", "until"),
    ("who=abc", "who"), ("who=999999", "who"), ("limit=0", "limit"), ("limit=201", "limit"), ("after=40", "after"), ("after=nonsense", "after"),
    ("after=2026-10-05T12:00:00~0~1", "after"), ("after=2026-10-05T12:00:00%2B00:00~99~1", "after"), ("limit=ten", "limit"),
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
    long_after = history.cursor_text((timezone.now() + timedelta(minutes=1), 0, 0))  # everything is older than this
    older = client.get(LOG + "?" + urlencode({"after": long_after}), **hx("log-more"))
    assert older.status_code == 200 and "Technician → CE manager" in older.content.decode() and "Contracts: View → Edit" not in older.content.decode()
    assert "Technician → CE manager" in csv_text(client.get(CSV)) and "CE-10001" in client.get(PRINT).content.decode()
    data = client.get(API).json()
    assert data["results"][0]["changes"][0]["after"] == "Technician → CE manager"
    assert "Contracts: View → Edit" not in str(data)
    with tenant_context(kim.tenant):
        assert AccessEvent.objects.filter(action=AccessEvent.Action.ROLE_CHANGED, by=kim, user=tom).count() == 1
