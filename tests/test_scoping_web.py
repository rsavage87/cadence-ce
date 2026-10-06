"""Slice 16, part B: the web screens keep a scoped user to their share of the facility (apps.workorders.scoping), closed by default.

A vendor technician (company scope) and a clinical requester (department scope) sign in to a facility with devices in two units,
vendor work for two companies, in-house work, and portal requests, next to another facility. Every web URL that shows devices or
work orders is requested, with and without filters; no device tag, work-order number, or problem outside their share may appear in
any of it, one out of it is a 404, and every screen that is not narrowed to them is a 403, whatever their levels. A facility-wide
role still sees everything, and a scoped user without a company or a matching unit sees nothing."""
import json
import uuid
from datetime import date, timedelta

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts.models import DataScope, Level, Module, Role, create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.web.templatetags.scoping_tags import HIDDEN, scoped_text
from apps.workorders.models import Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
A_VENDOR, B_VENDOR = "Hamilton Medical field service", "Acme Biomed"  # how work orders name company A's and company B's service
TAGS = ["ICU-VENT-1", "ICU-PUMP-2", "MED-VENT-3", "MED-PUMP-4", "MED-PUMP-5"]
THEIR_TAG, THEIR_PROBLEM = "THEIRS-VENT-9", "Problem from the other facility"

# Who sees what: the devices and the work orders (keys of world["wo"]) in each persona's share.
PERSONAS = {
    # company A, typed in another letter case than the work orders name it
    "vendor": {"slug": "vendor", "fields": {"company": "hamilton MEDICAL"}, "tags": {"ICU-VENT-1", "MED-VENT-3"}, "wos": {"a_icu", "a_med"}},
    # the ICU, typed in another letter case than the department's name
    "requester": {"slug": "requester", "fields": {"department": "icu"}, "tags": {"ICU-VENT-1", "ICU-PUMP-2"},
                  "wos": {"a_icu", "b_icu", "house_icu", "done_icu", "portal_icu"}},
}

# The views that admit a scoped user (web_view's scoped=True), by URL name. Each narrows what it shows; this test requests them all.
ADMITS_SCOPED = {"overview", "password_change", "search", "asset_search", "equipment", "asset", "workorders", "wo", "wo_status", "wo_note",
                 "wo_labor_add", "wo_labor_delete", "wo_part_add", "wo_part_delete", "wo_complete", "equipment_csv", "workorders_csv", "labels",
                 "wo_print", "scan",
                 # Slice 22: the person's own facilities. Nothing of this facility's; All facilities reads each facility with the person's
                 # account there (tests/test_all_facilities.py)
                 "facility_switch", "facility_join", "all_facilities"}
SIGNED_OUT = {"login", "logout", "password_reset", "password_reset_sent", "password_reset_complete", "password_reset_confirm", "invite_accept"}


def problem(key) -> str:
    return f"Problem {key}."


@pytest.fixture
def world(ctx, dept, vent_model, pump_model, techs, other_tenant):
    med = Department.objects.create(name="Med-Surg 4")
    today = date.today()

    def device(tag, model, department):
        return Asset.objects.create(tag=tag, device_model=model, department=department, acquisition_cost=5000, installed_on=today - timedelta(days=900),
                                    next_pm_on=today + timedelta(days=60))

    d = {"ICU-VENT-1": device("ICU-VENT-1", vent_model, dept), "ICU-PUMP-2": device("ICU-PUMP-2", pump_model, dept),
         "MED-VENT-3": device("MED-VENT-3", vent_model, med), "MED-PUMP-4": device("MED-PUMP-4", pump_model, med),
         "MED-PUMP-5": device("MED-PUMP-5", pump_model, med)}

    def wo(key, tag, *, vendor="", tech=None, source=Source.MANUAL, done=False):
        w = create_work_order(asset=d[tag], type=WoType.REPAIR, priority="normal", problem=problem(key), requester="RN Lee", source=source)
        if vendor or tech:
            assign(w, technician=tech, vendor_name=vendor)
        if done:
            change_status(w, WoStatus.IN_PROGRESS)
            change_status(w, WoStatus.COMPLETED)
        return w

    wos = {
        "a_icu": wo("a_icu", "ICU-VENT-1", vendor=A_VENDOR), "a_med": wo("a_med", "MED-VENT-3", vendor=A_VENDOR),
        "b_icu": wo("b_icu", "ICU-PUMP-2", vendor=B_VENDOR), "b_med": wo("b_med", "MED-PUMP-4", vendor=B_VENDOR),
        "house_icu": wo("house_icu", "ICU-VENT-1", tech=techs["dana"]), "house_med": wo("house_med", "MED-PUMP-5", tech=techs["tom"]),
        "done_icu": wo("done_icu", "ICU-PUMP-2", tech=techs["dana"], done=True), "done_med": wo("done_med", "MED-PUMP-4", tech=techs["tom"], done=True),
        "portal_icu": wo("portal_icu", "ICU-PUMP-2", source=Source.PORTAL), "portal_med": wo("portal_med", "MED-PUMP-5", source=Source.PORTAL),
    }
    # Another facility, with work for the same company: tenant isolation holds whatever the scope.
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-C6", description="Ventilator", category="Ventilators")
        theirs = Asset.objects.create(tag=THEIR_TAG, device_model=model, department=Department.objects.create(name="ICU"))
        create_work_order(asset=theirs, type=WoType.REPAIR, priority="normal", problem=THEIR_PROBLEM, vendor_service=True, vendor_name=A_VENDOR)
    return {"devices": d, "wo": wos}


def sign_in(client, make_user, slug, **fields):
    user = make_user(slug)
    for name, value in fields.items():
        setattr(user, name, value)
    user.save()
    client.force_login(user)
    return user


def body_of(r) -> str:
    if getattr(r, "streaming", False):
        return b"".join(r.streaming_content).decode("utf-8-sig")
    return r.content.decode()


def hidden(world, persona) -> list[str]:
    """Every tag, work-order number, and problem outside the persona's share, and the other facility's."""
    out = [t for t in TAGS if t not in persona["tags"]] + [THEIR_TAG, THEIR_PROBLEM]
    for key, w in world["wo"].items():
        if key not in persona["wos"]:
            out += [w.number, problem(key)]
    return out


def assert_no_leak(body: str, world, persona, where: str, echoed: str = ""):
    """`echoed`: the user's own search text, which the page shows back in its search box."""
    if echoed:
        body = body.replace(echoed, "")
    leaks = [s for s in hidden(world, persona) if s in body]
    assert leaks == [], f"{where} shows {leaks}"


def get(client, url, **headers):
    r = client.get(url, follow=True, **headers)
    assert r.status_code == 200, (url, r.status_code)
    return r


# --- every URL that shows devices or work orders ------------------------------------------------------------------------------

EQUIPMENT_FILTERS = ["", "?q=PUMP", "?q=Med-Surg", "?dept=Med-Surg%204", "?dept=ICU", "?status=active", "?status=in_service", "?risk=high",
                     "?category=Infusion%20pumps", "?support=in_house", "?bucket=compliant", "?overdue=1", "?sort=location&dir=desc", "?sort=cost",
                     "?page=2"]
WO_FILTERS = ["", "?open=0", "?open=0&q=Problem", "?q=MED", "?type=repair&open=0", "?status=completed", "?assigned=unassigned",
              "?mode=board", "?mode=board&q=Problem", "?page=3&open=0"]


@pytest.mark.parametrize("who", sorted(PERSONAS))
def test_nothing_outside_the_share_shows_anywhere(client, make_user, world, who):
    persona = PERSONAS[who]
    sign_in(client, make_user, persona["slug"], **persona["fields"])
    mine = {k: w for k, w in world["wo"].items() if k in persona["wos"]}
    seen_tags, seen_numbers = set(), set()

    def scan(url, *, echoed="", **headers):
        body = body_of(get(client, url, **headers))
        assert_no_leak(body, world, persona, url, echoed)
        body = body.replace(echoed, "") if echoed else body
        seen_tags.update(t for t in TAGS if t in body)
        seen_numbers.update(w.number for w in mine.values() if w.number in body)
        return body

    # the lists, their filters and sorts, their partials, and the exports and label sheets that carry the same filters
    for f in EQUIPMENT_FILTERS:
        scan(f"/equipment/{f}")
        scan(f"/equipment/{f}", HTTP_HX_TARGET="eq-table", **HX)
        scan(f"/export/equipment.csv{f}")
        scan(f"/print/labels/{f}")
    for f in WO_FILTERS:
        scan(f"/work-orders/{f}")
        scan(f"/work-orders/{f}", HTTP_HX_TARGET="wo-body", **HX)
        scan(f"/export/work-orders.csv{f}")
    scan("/print/labels/?layout=label")
    # the drawers in their share: every tab a device drawer may be asked for, as a partial and as a full page; the work-order prints
    for tag in persona["tags"]:
        for t in ("", "?tab=overview", "?tab=wo", "?tab=pm", "?tab=costs", "?tab=recalls"):
            scan(f"/equipment/{tag}/{t}", **HX)
            scan(f"/equipment/{tag}/{t}")
        scan(f"/print/labels/?tag={tag}")
    for w in mine.values():
        scan(f"/work-orders/{w.number}/", **HX)
        scan(f"/work-orders/{w.number}/")
        scan(f"/print/work-orders/{w.number}/")
    # search: the topbar's exact matches open theirs; anyone else's tag or number is only text searched in their own list
    for tag in TAGS + [THEIR_TAG]:
        scan(f"/search/?q={tag}", echoed=tag)
        scan(f"/search/assets/?asset_q={tag}", echoed=tag)
    for w in world["wo"].values():
        scan(f"/search/?q={w.number}", echoed=w.number)
    for q in ("PUMP", "VENT", "MED", "ICU", "Hamilton", "Alaris"):
        scan(f"/search/assets/?asset_q={q}")
    scan("/account/password/")
    # everything theirs did show somewhere
    assert seen_tags == persona["tags"] and seen_numbers == {w.number for w in mine.values()}


@pytest.mark.parametrize("who", sorted(PERSONAS))
def test_a_record_outside_the_share_is_a_404(client, make_user, world, who):
    persona = PERSONAS[who]
    sign_in(client, make_user, persona["slug"], **persona["fields"])
    for tag in [t for t in TAGS if t not in persona["tags"]] + [THEIR_TAG]:
        for url in (f"/equipment/{tag}/", f"/equipment/{tag}/?tab=wo", f"/print/labels/?tag={tag}"):
            assert client.get(url, **HX).status_code == 404, url
            assert client.get(url).status_code == 404, url
    for key, w in world["wo"].items():
        if key in persona["wos"]:
            continue
        for url in (f"/work-orders/{w.number}/", f"/print/work-orders/{w.number}/"):
            assert client.get(url, **HX).status_code == 404, url
            assert client.get(url).status_code == 404, url


def test_a_vendor_cannot_act_on_another_companys_work_order(client, make_user, world):
    sign_in(client, make_user, "vendor", company="Hamilton Medical")  # work orders: Edit
    for key in ("b_icu", "house_icu", "portal_med"):
        w = world["wo"][key]
        base = f"/work-orders/{w.number}"
        assert client.post(f"{base}/status/", {"to": WoStatus.IN_PROGRESS}, **HX).status_code == 404
        assert client.post(f"{base}/notes/", {"text": "Not mine"}, **HX).status_code == 404
        assert client.get(f"{base}/labor/", **HX).status_code == 404 and client.get(f"{base}/parts/", **HX).status_code == 404
        assert client.post(f"{base}/labor/", {"worked_on": date.today().isoformat(), "hours": "1"}, **HX).status_code == 404
        assert client.post(f"{base}/parts/", {"description": "Fuse", "quantity": "1", "unit_cost": "2"}, **HX).status_code == 404
        assert client.get(f"{base}/complete/", **HX).status_code == 404
        assert client.post(f"{base}/complete/", {"resolution": "Fixed"}, **HX).status_code == 404
        w.refresh_from_db()
        assert w.status in (WoStatus.OPEN, WoStatus.COMPLETED) and not w.notes.exists() and not w.labor_lines.exists() and not w.part_lines.exists()


def test_a_vendor_works_their_own_companys_work_order(client, make_user, world):
    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    w = world["wo"]["a_med"]
    base = f"/work-orders/{w.number}"
    r = client.post(f"{base}/status/", {"to": WoStatus.IN_PROGRESS}, **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"]["value"] == f"{w.number}: in progress"
    assert client.post(f"{base}/notes/", {"text": "On site"}, **HX).status_code == 200
    assert client.post(f"{base}/labor/", {"worked_on": date.today().isoformat(), "hours": "2"}, **HX)["HX-Retarget"] == "#drawer"
    assert client.post(f"{base}/parts/", {"description": "Flow sensor", "quantity": "1", "unit_cost": "120"}, **HX)["HX-Retarget"] == "#drawer"
    r = client.post(f"{base}/complete/", {"resolution": "Replaced the flow sensor"}, **HX)
    w.refresh_from_db()
    assert r.status_code == 200 and w.status == WoStatus.COMPLETED and w.notes.count() == 1 and w.labor_lines.count() == 1
    assert client.get(f"/print/work-orders/{w.number}/").status_code == 200


# --- closed by default ---------------------------------------------------------------------------------------------------------

def web_urls():
    """(name, callback, kwargs) for every URL in apps/web/urls*.py."""
    dummy = {"string": "x", "uuid": str(uuid.uuid4()), "int": 1, "slug": "cosr", "path": "x"}
    out = []

    def walk(patterns):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns)
            elif isinstance(p, URLPattern) and p.name:
                kwargs = {name: dummy[type(conv).__name__.replace("Converter", "").lower()] for name, conv in p.pattern.converters.items()}
                out.append((p.name, p.callback, kwargs))

    web = next(p for p in get_resolver().url_patterns if isinstance(p, URLResolver) and p.namespace == "web")
    walk(web.url_patterns)
    return out


def test_every_web_view_says_whether_it_admits_scoped_users():
    names = {name: getattr(callback, "admits_scoped", None) for name, callback, _ in web_urls()}
    assert {n for n, v in names.items() if v is None} == SIGNED_OUT  # not web_view: the signed-out pages
    assert {n for n, v in names.items() if v is True} == ADMITS_SCOPED


def full_scoped_user(tenant, scope, **fields):
    from apps.accounts.models import User

    role = Role.objects.create(name=f"Scoped {scope}", slug=f"scoped-{scope}", scope=scope)
    role.set_levels({m: Level.FULL for m in Module.values})
    return User.objects.create_user(username=f"{scope}-lead@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **fields)


@pytest.mark.parametrize("who", ["vendor", "requester", "full-company", "full-department"])
def test_every_other_screen_refuses_a_scoped_user_whatever_their_levels(client, make_user, tenant, world, who):
    if who in PERSONAS:
        sign_in(client, make_user, PERSONAS[who]["slug"], **PERSONAS[who]["fields"])
    elif who == "full-company":
        client.force_login(full_scoped_user(tenant, DataScope.COMPANY, company="Hamilton Medical"))
    else:
        client.force_login(full_scoped_user(tenant, DataScope.DEPARTMENT, department="ICU"))
    refused = [(name, kwargs) for name, callback, kwargs in web_urls() if getattr(callback, "admits_scoped", None) is False]
    assert {"pm", "contracts", "recalls", "reports", "users", "roles", "settings", "credentials", "wo_new", "wo_assign", "asset_new", "asset_edit",
            "asset_status", "route_sheets", "report_print", "contracts_csv", "pm_model"} <= {name for name, _ in refused}
    for name, kwargs in refused:
        url = reverse(f"web:{name}", kwargs=kwargs)
        assert client.post(url, **HX).status_code == 403, url
        assert client.get(url, **HX).status_code in (403, 405), url  # 405: POST-only
        assert client.get(url).status_code in (403, 405), url
    # nothing changed on the way
    assert not WorkOrder.objects.filter(status=WoStatus.CANCELLED).exists() and WorkOrder.objects.count() == len(world["wo"])


@pytest.mark.parametrize("who", ["full-company", "full-department"])
def test_full_levels_do_not_widen_a_scoped_role(client, tenant, world, who):
    scope = DataScope.COMPANY if who == "full-company" else DataScope.DEPARTMENT
    persona = PERSONAS["vendor" if scope == DataScope.COMPANY else "requester"]
    client.force_login(full_scoped_user(tenant, scope, **persona["fields"]))
    r = get(client, "/")
    assert r.redirect_chain == [("/equipment/", 302)]
    assert [i["key"] for i in r.context["shell"]["nav"]] == ["equipment", "workorders"]
    assert {a.tag for a in r.context["page"]} == persona["tags"] and not r.context["can_add_device"]
    body = body_of(r)
    assert "Add device" not in body and "/pm/" not in body and "/reports/" not in body and "/contracts/" not in body
    tag = sorted(persona["tags"])[0]
    drawer = client.get(f"/equipment/{tag}/?tab=costs", **HX)
    html = drawer.content.decode()
    # Overview and Work orders only: no PM, Costs, or Recalls tab, and none of the changes the drawer offers
    assert drawer.context["tabs"] == ["overview", "wo"] and drawer.context["tab"] == "overview"
    assert not drawer.context["can_create_wo"] and not drawer.context["can_edit_device"] and drawer.context["status_actions"] == []
    assert "Edit details" not in html and "New work order" not in html and "/contracts/" not in html and "/pm/models/" not in html
    w = next(w for k, w in world["wo"].items() if k in persona["wos"])
    html = client.get(f"/work-orders/{w.number}/", **HX).content.decode()
    assert "/assign/" not in html and "/recalls/" not in html
    page = client.get("/work-orders/")
    assert not page.context["can_create"] and "New work order" not in page.content.decode()
    for key in persona["wos"]:
        assert_no_leak(client.get(f"/work-orders/{world['wo'][key].number}/", **HX).content.decode(), world, persona, key)


# --- the page heads and the nav ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", sorted(PERSONAS))
def test_counts_and_badges_are_the_users_own(client, make_user, world, who):
    persona = PERSONAS[who]
    sign_in(client, make_user, persona["slug"], **persona["fields"])
    mine = [w for k, w in world["wo"].items() if k in persona["wos"]]
    open_mine = [w for w in mine if w.status != WoStatus.COMPLETED]
    r = get(client, "/equipment/")
    assert r.context["summary"] == {"total": len(persona["tags"]), "active": len(persona["tags"]), "under_contract": 0}
    assert set(r.context["options"]["departments"]) == {Asset.objects.get(tag=t).department.name for t in persona["tags"]}
    nav = {i["key"]: i for i in r.context["shell"]["nav"]}
    assert list(nav) == ["equipment", "workorders"]
    assert nav["equipment"]["count"] == len(persona["tags"]) and nav["workorders"]["count"] == len(open_mine)
    assert nav["workorders"]["hot"] is False  # portal requests wait in the facility (one on the requester's own unit): a CE manager's to assign
    r = get(client, "/work-orders/")
    assert r.context["open_count"] == len(open_mine) and r.context["done_7d"] == len(mine) - len(open_mine)
    assert r.context["unassigned_portal"] == 0 and "waiting for assignment" not in body_of(r)
    board = get(client, "/work-orders/?mode=board")
    assert sum(c["count"] for c in board.context["columns"]) == len(mine)
    # the device drawer's figures are over their work orders on it
    vent = world["devices"]["ICU-VENT-1"]
    drawer = client.get(f"/equipment/{vent.tag}/?tab=wo", **HX)
    shown = {w.number for w in drawer.context["summary"]["work_orders"]}
    assert shown == {w.number for w in mine if w.asset_id == vent.pk}
    assert f"Work orders ({len(shown)})" in drawer.content.decode()


def test_the_equipment_export_counts_only_their_open_work_orders(client, make_user, world):
    from tests.csvutil import csv_rows

    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    header, *rows = csv_rows(client.get("/export/equipment.csv"))
    by_tag = {row[0]: dict(zip(header, row)) for row in rows}
    assert set(by_tag) == PERSONAS["vendor"]["tags"]
    assert by_tag["ICU-VENT-1"]["Open work orders"] == "1"  # theirs, not the in-house one on the same device


# --- the home page --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", sorted(PERSONAS))
def test_the_home_page_sends_a_scoped_user_to_their_equipment(client, make_user, world, who):
    sign_in(client, make_user, PERSONAS[who]["slug"], **PERSONAS[who]["fields"])
    r = client.get("/")
    assert r.status_code == 302 and r["Location"] == "/equipment/"


def test_a_scoped_role_with_only_work_orders_lands_there(client, tenant, world):
    from apps.accounts.models import User

    role = Role.objects.create(name="Vendor dispatch", slug="vendor-dispatch", scope=DataScope.COMPANY)
    role.set_levels({Module.WORKORDERS: Level.VIEW, Module.REPORTS: Level.FULL, Module.PM: Level.FULL})
    client.force_login(User.objects.create_user(username="dispatch@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                                company="Acme Biomed"))
    r = client.get("/")
    assert r.status_code == 302 and r["Location"] == "/work-orders/"
    assert [i["key"] for i in client.get("/work-orders/").context["shell"]["nav"]] == ["workorders"]
    assert client.get("/reports/").status_code == 403 and client.get("/pm/").status_code == 403


# --- the everyone-else and the nobody cases ------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["technician", "director"])
def test_a_facility_wide_role_still_sees_everything(client, make_user, world, slug):
    sign_in(client, make_user, slug)
    assert {a.tag for a in get(client, "/equipment/").context["page"]} == set(TAGS)
    numbers = {w.number for w in world["wo"].values()}
    assert {w.number for w in get(client, "/work-orders/?open=0").context["page"]} == numbers
    csv = body_of(get(client, "/export/work-orders.csv?open=0"))
    assert all(n in csv for n in numbers) and all(problem(k) in csv for k in world["wo"])
    r = get(client, "/work-orders/")
    assert r.context["unassigned_portal"] == 2 and "waiting for assignment" in body_of(r)
    nav = {i["key"]: i for i in r.context["shell"]["nav"]}
    assert nav["equipment"]["count"] == len(TAGS) and nav["workorders"]["hot"] is True
    for tag in TAGS:
        assert client.get(f"/equipment/{tag}/", **HX).status_code == 200
    assert client.get(f"/equipment/{THEIR_TAG}/", **HX).status_code == 404


@pytest.mark.parametrize("slug, fields", [("vendor", {}), ("vendor", {"company": "   "}), ("vendor", {"company": "Nobody Inc"}),
                                          ("requester", {}), ("requester", {"department": "Nowhere"})])
def test_a_scoped_user_without_a_company_or_unit_sees_nothing(client, make_user, world, slug, fields):
    sign_in(client, make_user, slug, **fields)
    r = get(client, "/equipment/")
    assert r.context["page"].paginator.count == 0 and r.context["summary"]["total"] == 0
    assert r.context["options"]["departments"] == [] and r.context["options"]["categories"] == []
    assert {i["key"]: i["count"] for i in r.context["shell"]["nav"]} == {"equipment": 0, "workorders": 0}
    assert get(client, "/work-orders/?open=0").context["page"].paginator.count == 0
    assert sum(c["count"] for c in get(client, "/work-orders/?mode=board").context["columns"]) == 0
    nobody = {"slug": slug, "tags": set(), "wos": set()}
    for url in ("/export/equipment.csv", "/export/work-orders.csv?open=0", "/print/labels/", "/search/assets/?asset_q=PUMP"):
        assert_no_leak(body_of(get(client, url)), world, nobody, url)
    for tag in TAGS:
        assert client.get(f"/equipment/{tag}/", **HX).status_code == 404
    for w in world["wo"].values():
        assert client.get(f"/work-orders/{w.number}/", **HX).status_code == 404
        assert client.get(f"/search/?q={w.number}")["Location"].startswith("/equipment/?q=")


# --- through a work order in their share ------------------------------------------------------------------------------------------

def test_a_failed_vendor_pm_without_repair_coverage_does_not_lead_to_the_in_house_repair(client, make_user, world):
    """A vendor's failed PM on a device whose contract covers PM only opens a repair for CE to assign: the vendor neither reaches it
    nor reads its number, in the toast, the PM's drawer (its history names the repair), the print, the device drawer, or the export.
    The unit's requester sees both, linked."""
    from apps.contracts.models import Contract, ContractType, Coverage

    vent = world["devices"]["ICU-VENT-1"]
    today = date.today()
    vent.contract = Contract.objects.create(reference="SC-PM-ONLY", vendor=A_VENDOR, type=ContractType.OEM, coverage=Coverage.PM_ONLY,
                                            start_on=today - timedelta(days=100), end_on=today + timedelta(days=200))
    vent.save()
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled preventive maintenance", source=Source.PM_PLANNER)
    assign(pm, vendor_name=A_VENDOR)
    change_status(pm, WoStatus.IN_PROGRESS)
    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    r = client.post(f"/work-orders/{pm.number}/complete/", {"resolution": "Flow sensor reads 20% high", "pm_result": "fail", "shown_tag_out": "1"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer", r.content.decode()[:500]
    repair = WorkOrder.objects.get(follow_up_of=pm)
    assert not repair.vendor_service and repair.assigned_to_id is None  # CE's to assign: outside the vendor's share
    toast = json.loads(r["HX-Trigger"])["toast"]["value"]
    assert toast == f"{pm.number} completed: PM failed; a repair work order opened"
    bodies = {"drawer": r.content.decode(), "drawer again": client.get(f"/work-orders/{pm.number}/", **HX).content.decode(),
              "print": client.get(f"/print/work-orders/{pm.number}/").content.decode(),
              "device": client.get(f"/equipment/{vent.tag}/?tab=wo", **HX).content.decode(),
              "export": body_of(client.get("/export/work-orders.csv?open=0"))}
    for where, body in bodies.items():
        assert repair.number not in body, where
    assert HIDDEN in bodies["drawer"]  # the history's "PM failed; WO-.. opened for the repair" reads "another work order"
    assert "Repair <span>opened from this PM</span>" not in bodies["drawer"]
    assert client.get(f"/work-orders/{repair.number}/", **HX).status_code == 404
    assert client.get(f"/print/work-orders/{repair.number}/").status_code == 404

    client.logout()
    sign_in(client, make_user, "requester", department="ICU")  # the unit sees every work order on its devices
    drawer = client.get(f"/work-orders/{pm.number}/", **HX).content.decode()
    assert f'hx-get="/work-orders/{repair.number}/"' in drawer and HIDDEN not in drawer
    repair_drawer = client.get(f"/work-orders/{repair.number}/", **HX).content.decode()
    assert f"PM {pm.number} failed" in repair_drawer and f'hx-get="/work-orders/{pm.number}/"' in repair_drawer


def test_a_repair_in_the_share_does_not_name_or_link_its_pm_outside_it(client, make_user, world, techs):
    """An in-house PM fails and CE gives its repair to the vendor: the vendor's repair says which PM it came from only as text."""
    from apps.workorders.completion import complete_work_order

    vent = world["devices"]["MED-VENT-3"]
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled preventive maintenance", source=Source.PM_PLANNER)
    assign(pm, technician=techs["dana"])
    change_status(pm, WoStatus.IN_PROGRESS)
    done = complete_work_order(pm, resolution="Flow sensor reads 20% high", pm_result="fail", tag_out=False)
    repair = done.follow_up
    assign(repair, vendor_name=A_VENDOR)
    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    for url in (f"/work-orders/{repair.number}/", f"/print/work-orders/{repair.number}/", f"/equipment/{vent.tag}/?tab=wo", "/export/work-orders.csv"):
        body = body_of(client.get(url, **HX))
        assert pm.number not in body and HIDDEN in body, url
    assert client.get(f"/work-orders/{pm.number}/", **HX).status_code == 404


def test_scoped_text_leaves_facility_users_and_their_own_numbers_alone(make_user, world):
    a, b = world["wo"]["a_icu"], world["wo"]["b_icu"]
    text = f"See {a.number} and {b.number}."
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    assert scoped_text(vendor, text) == f"See {a.number} and {HIDDEN}."
    assert scoped_text(make_user("technician"), text) == text
    assert scoped_text(vendor, "") == "" and scoped_text(vendor, "No numbers") == "No numbers"


def test_a_failed_vendor_pm_with_repair_coverage_sends_the_repair_to_the_vendor(client, make_user, world):
    """No contract (the manufacturer's field service, time and materials): the vendor who found the failure does the repair, and it is
    in their share: they reach it, and the toast names it."""
    vent = world["devices"]["ICU-VENT-1"]
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled preventive maintenance", source=Source.PM_PLANNER)
    assign(pm, vendor_name=A_VENDOR)
    change_status(pm, WoStatus.IN_PROGRESS)
    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    r = client.post(f"/work-orders/{pm.number}/complete/", {"resolution": "Flow sensor reads 20% high", "pm_result": "fail", "shown_tag_out": "1"}, **HX)
    repair = WorkOrder.objects.get(follow_up_of=pm)
    assert repair.vendor_service and repair.vendor_name == A_VENDOR
    assert json.loads(r["HX-Trigger"])["toast"]["value"] == f"{pm.number} completed: PM failed; {repair.number} opened for the repair"
    assert client.get(f"/work-orders/{repair.number}/", **HX).status_code == 200
