"""Web UI (slice 4): sign-in, tenant isolation through URLs, HTMX partials, and server-side permission checks on every action."""
from datetime import date, timedelta

import pytest

from apps.accounts.models import create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoStatus
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}


def hx(target):
    return {**HX, "HTTP_HX_TARGET": target}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def wo(ctx, vent):
    return create_work_order(asset=vent, type="repair", priority="high", problem="Low tidal volume alarm")


# --- sign-in and landing ------------------------------------------------------------------------

def test_every_screen_requires_sign_in(client, db):
    for url in ["/", "/equipment/", "/work-orders/", "/work-orders/new/", "/search/?q=x", "/contracts/", "/users/", "/users/credentials/", "/recalls/",
                "/reports/", "/reports/cosr.csv"]:
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login/"), url


def test_login_page_renders(client, db):
    r = client.get("/login/")
    assert r.status_code == 200 and b"Sign in" in r.content


def test_superuser_without_tenant_gets_a_pointer_not_an_error(client, django_user_model):
    client.force_login(django_user_model.objects.create_superuser("root", "root@example.com", "Test-Pass-2026-x"))
    r = client.get("/equipment/")
    assert r.status_code == 200 and b"No tenant selected" in r.content


def test_overview_shows_kpis_and_attention(client, signed_in, wo, vent):
    signed_in("director")
    vent.next_pm_on = date.today() - timedelta(days=3)
    vent.save()
    r = client.get("/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Fleet overview" in body and "PM completion on time" in body and f"{vent.tag} · ICU ventilator" in body


def test_overview_ignores_future_months(client, signed_in):
    signed_in("director")
    r = client.get("/?y=2999&m=1")
    assert r.status_code == 200 and r.context["year"] == date.today().year


def test_requester_without_reports_lands_on_equipment(client, signed_in):
    signed_in("requester")
    r = client.get("/")
    assert r.status_code == 302 and r["Location"] == "/equipment/"


def test_nav_only_lists_screens_the_role_can_view(client, signed_in):
    signed_in("requester")
    r = client.get("/equipment/")
    keys = [item["key"] for item in r.context["shell"]["nav"]]
    assert keys == ["equipment", "workorders"]  # requester has Request on work orders, no reports


# --- equipment ----------------------------------------------------------------------------------

def test_equipment_filters_and_partial(client, signed_in, vent, pump):
    signed_in("technician")
    r = client.get("/equipment/?risk=life_support")
    assert r.status_code == 200 and [a.tag for a in r.context["page"]] == [vent.tag]
    partial = client.get("/equipment/?risk=high", **hx("eq-table"))
    assert partial.status_code == 200 and b"<html" not in partial.content and pump.tag.encode() in partial.content and vent.tag.encode() not in partial.content


def test_unknown_filter_values_are_ignored(client, signed_in, vent, pump):
    signed_in("technician")
    r = client.get("/equipment/?risk=bogus&sort=drop_table&bucket=nope&dept=Nowhere")
    assert r.status_code == 200 and r.context["page"].paginator.count == 2


def test_asset_drawer_partial_and_full_page(client, signed_in, vent, wo):
    signed_in("technician")
    partial = client.get(f"/equipment/{vent.tag}/", **HX)
    assert partial.status_code == 200 and b"<html" not in partial.content and wo.number.encode() in partial.content
    page = client.get(f"/equipment/{vent.tag}/")
    assert page.status_code == 200 and b'class="drawer open"' in page.content and b"<html" in page.content


def test_other_tenants_records_are_not_found(client, tenant, other_tenant, make_user, wo, vent):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d)
    client.force_login(make_user("director", tenant_=other_tenant))
    assert client.get(f"/equipment/{vent.tag}/").status_code == 404
    assert client.get(f"/work-orders/{wo.number}/").status_code == 404
    assert client.post(f"/work-orders/{wo.number}/status/", {"to": "in_progress"}).status_code == 404
    assert client.get(f"/equipment/{theirs.tag}/").status_code == 200
    assert [a.tag for a in client.get("/equipment/").context["page"]] == ["THEIRS-1"]


def test_view_level_is_enough_to_open_the_device_drawer_but_not_to_create(client, signed_in, vent):
    signed_in("requester")  # equipment: View, work orders: Request
    r = client.get(f"/equipment/{vent.tag}/", **HX)
    assert r.status_code == 200 and not r.context["can_create_wo"]


# --- work orders --------------------------------------------------------------------------------

def test_work_order_list_board_and_drawer(client, signed_in, wo):
    signed_in("technician")
    assert wo.number.encode() in client.get("/work-orders/").content
    board = client.get("/work-orders/?mode=board")
    assert board.context["columns"][0]["count"] == 1
    drawer = client.get(f"/work-orders/{wo.number}/", **HX)
    assert drawer.status_code == 200 and b"Low tidal volume alarm" in drawer.content


def test_open_only_checkbox_can_be_turned_off(client, signed_in, wo, pump):
    signed_in("technician")
    done = create_work_order(asset=pump, type="repair", priority="normal", problem="Done")
    change_status(done, "in_progress")
    change_status(done, "completed")
    assert client.get("/work-orders/").context["page"].paginator.count == 1
    assert client.get("/work-orders/?open=0").context["page"].paginator.count == 2
    assert client.get("/work-orders/?open=0&open=1").context["page"].paginator.count == 1


def test_unassigned_work_orders_offer_no_status_buttons(client, signed_in, wo):
    signed_in("director")
    r = client.get(f"/work-orders/{wo.number}/", **HX)
    assert r.context["actions"] == [] and r.context["can_assign"]


def test_technician_moves_work_but_cannot_close_it(client, signed_in, wo, techs):
    signed_in("technician")
    assign(wo, technician=techs["dana"])
    r = client.post(f"/work-orders/{wo.number}/status/", {"to": "in_progress"}, **HX)
    assert r.status_code == 200 and "wo-changed" in r["HX-Trigger"]
    client.post(f"/work-orders/{wo.number}/status/", {"to": "completed"}, **HX)
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.completed_on == date.today()
    assert client.post(f"/work-orders/{wo.number}/status/", {"to": "closed"}, **HX).status_code == 403
    assert "Review and close" not in client.get(f"/work-orders/{wo.number}/", **HX).content.decode()


def test_manager_closes_work(client, signed_in, wo, techs):
    signed_in("manager")
    assign(wo, technician=techs["dana"])
    for to in ("in_progress", "completed", "closed"):
        client.post(f"/work-orders/{wo.number}/status/", {"to": to}, **HX)
    wo.refresh_from_db()
    assert wo.status == WoStatus.CLOSED


def test_illegal_transition_is_reported_not_applied(client, signed_in, wo):
    signed_in("manager")
    r = client.post(f"/work-orders/{wo.number}/status/", {"to": "completed"}, **HX)
    wo.refresh_from_db()
    assert r.status_code == 200 and wo.status == WoStatus.OPEN and "Cannot move" in r["HX-Trigger"]


def test_read_only_roles_cannot_change_work_orders(client, signed_in, wo):
    signed_in("analyst")  # work orders: View
    assert client.get(f"/work-orders/{wo.number}/", **HX).status_code == 200
    assert client.post(f"/work-orders/{wo.number}/status/", {"to": "in_progress"}).status_code == 403
    assert client.post(f"/work-orders/{wo.number}/notes/", {"text": "hi"}).status_code == 403
    assert client.get("/work-orders/new/").status_code == 403


def test_status_changes_need_post(client, signed_in, wo):
    signed_in("manager")
    assert client.get(f"/work-orders/{wo.number}/status/?to=in_progress").status_code == 405


def test_assignment_needs_approve_and_flags_overrides(client, signed_in, wo, techs):
    signed_in("technician")
    assert client.post(f"/work-orders/{wo.number}/assign/", {"assignee": str(techs["dana"].id)}).status_code == 403
    signed_in("manager")
    r = client.post(f"/work-orders/{wo.number}/assign/", {"assignee": str(techs["tom"].id)}, **HX)
    wo.refresh_from_db()
    assert r.status_code == 200 and wo.assigned_to == techs["tom"] and "override" in wo.status_history.last().note
    client.post(f"/work-orders/{wo.number}/assign/", {"assignee": "vendor"}, **HX)
    wo.refresh_from_db()
    assert wo.vendor_service and wo.assigned_to is None and wo.vendor_name == "Hamilton Medical field service"


def test_assignment_rejects_garbage(client, signed_in, wo):
    signed_in("manager")
    r = client.post(f"/work-orders/{wo.number}/assign/", {"assignee": "not-a-uuid"}, **HX)
    wo.refresh_from_db()
    assert r.status_code == 200 and wo.assigned_to is None and "Choose a technician" in r["HX-Trigger"]


def test_notes_are_added_through_the_service(client, signed_in, wo):
    user = signed_in("technician")
    client.post(f"/work-orders/{wo.number}/notes/", {"text": "  Ordered flow sensor "}, **HX)
    note = wo.notes.get()
    assert note.text == "Ordered flow sensor" and note.author == user


def test_create_work_order_with_assignment(client, signed_in, vent, techs):
    user = signed_in("manager")
    form = client.get(f"/work-orders/new/?asset={vent.tag}", **HX)
    assert form.status_code == 200 and "✓ Dana Whitfield" in form.content.decode()
    r = client.post("/work-orders/new/", {"asset": vent.tag, "type": "repair", "priority": "critical", "problem": "No power", "requester": "",
                                          "assignee": str(techs["dana"].id), "tag_out": "on"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    wo = WorkOrder.objects.get()
    assert wo.created_by == user and wo.assigned_to == techs["dana"] and wo.priority == "critical" and wo.due_on == date.today() + timedelta(days=1)
    vent.refresh_from_db()
    assert vent.status == "out_of_service"


def test_technician_can_create_but_not_assign(client, signed_in, vent, techs):
    signed_in("technician")
    assert b'name="assignee"' not in client.get(f"/work-orders/new/?asset={vent.tag}", **HX).content
    client.post("/work-orders/new/", {"asset": vent.tag, "type": "repair", "priority": "normal", "problem": "Alarm", "assignee": str(techs["dana"].id)}, **HX)
    assert WorkOrder.objects.get().assigned_to is None


def test_create_requires_a_real_device(client, signed_in, vent):
    signed_in("manager")
    r = client.post("/work-orders/new/", {"asset": "NOPE", "type": "repair", "priority": "normal", "problem": "x"}, **HX)
    assert r.status_code == 200 and "Choose a device" in r.content.decode() and not WorkOrder.objects.exists()


def test_device_picker(client, signed_in, vent):
    signed_in("technician")
    assert client.get("/search/assets/?asset_q=C", **HX).content == b""
    assert vent.tag.encode() in client.get(f"/search/assets/?asset_q={vent.tag[:5]}", **HX).content


def test_global_search_routes_exact_matches(client, signed_in, wo, vent):
    signed_in("technician")
    assert client.get(f"/search/?q={wo.number.lower()}")["Location"] == f"/work-orders/{wo.number}/"
    assert client.get(f"/search/?q={vent.tag.lower()}")["Location"] == f"/equipment/{vent.tag}/"
    assert client.get("/search/?q=hamilton g5")["Location"] == "/equipment/?q=hamilton+g5"


# --- the API follows the same rules ------------------------------------------------------------

def test_api_applies_the_same_close_and_assign_rules(client, signed_in, wo, techs):
    signed_in("technician")
    assert client.post(f"/api/v1/work-orders/{wo.id}/assign/", {"technician": str(techs["dana"].id)}).status_code == 403
    assign(wo, technician=techs["dana"])
    for to in ("in_progress", "completed"):
        assert client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": to}).status_code == 200
    assert client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "closed"}).status_code == 403
    signed_in("manager")
    assert client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "closed"}).status_code == 200


def test_api_patch_cannot_reassign_around_the_assign_action(client, signed_in, wo, techs):
    signed_in("director")
    r = client.patch(f"/api/v1/work-orders/{wo.id}/", {"assigned_to": str(techs["tom"].id)}, content_type="application/json")
    wo.refresh_from_db()
    assert r.status_code == 400 and wo.assigned_to is None
    ok = client.patch(f"/api/v1/work-orders/{wo.id}/", {"problem": "Low tidal volume alarm, intermittent"}, content_type="application/json")
    assert ok.status_code == 200


def test_self_swapping_wrappers_do_not_leak_outerhtml_to_their_children():
    """A wrapper that replaces itself (hx-swap="outerHTML") must not pass that swap down to rows and buttons that
    target #drawer or #modal-card; inherited outerHTML would replace the drawer element itself. hx-disinherit stops it."""
    import pathlib
    import re

    bad = []
    for path in pathlib.Path("apps/web/templates/web").glob("*.html"):
        for m in re.finditer(r"<(\w+)[^>]*hx-target=\"this\"[^>]*hx-swap=\"outerHTML\"[^>]*>", path.read_text()):
            if "hx-disinherit" not in m.group(0) or "hx-swap" not in m.group(0).split("hx-disinherit")[1]:
                bad.append(f"{path.name}: {m.group(0)[:80]}")
    assert bad == []


# --- recall integration points (slice 6): device drawer, work order drawer and list, nav ------------

def test_device_drawer_recalls_tab_lists_the_models_alerts(client, signed_in, pump, pump_recall, vent):
    signed_in("technician")  # recalls: View
    r = client.get(f"/equipment/{pump.tag}/?tab=recalls", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r.context["tab"] == "recalls" and "Recalls (1)" in body
    assert f'href="/recalls/?match={pump_recall.id}"' in body and "FDA Z-TEST-1 · Class II" in body and pump_recall.alert.title in body
    assert 'class="rail warn"' in body and "Needs action" in body and "Open recall" in body
    other = client.get(f"/equipment/{vent.tag}/?tab=recalls", **HX).content.decode()  # no alerts on this model
    assert "No alerts on file for this model." in other and "Open recall" not in other and ">Recalls</button>" in other


def test_device_drawer_open_recall_chip_only_while_action_is_needed(client, signed_in, pump, pump_recall):
    signed_in("technician")
    assert "Open recall" in client.get(f"/equipment/{pump.tag}/", **HX).content.decode()
    pump_recall.status = "closed"
    pump_recall.save()
    body = client.get(f"/equipment/{pump.tag}/?tab=recalls", **HX).content.decode()
    assert "Open recall" not in body and "Closed" in body and "Recalls (1)" in body


def test_device_drawer_hides_recalls_from_roles_without_recalls_view(client, signed_in, pump, pump_recall):
    signed_in("requester")  # equipment: View, recalls: None
    r = client.get(f"/equipment/{pump.tag}/?tab=recalls", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r.context["tab"] == "overview"  # the tab does not exist, so ?tab=recalls falls back
    assert "?tab=recalls" not in body and "Open recall" not in body and f"match={pump_recall.id}" not in body


def test_recall_work_order_shows_chip_and_row(client, signed_in, pump, pump_recall):
    signed_in("technician")
    wo = create_work_order(asset=pump, type="recall", priority="high", problem="Inspect keypad membrane", alert=pump_recall.alert)
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert '<span class="chip violet">Recall</span>' in body and "<dt>Recall</dt>" in body
    assert f'<a class="link" href="/recalls/?match={pump_recall.id}">FDA Z-TEST-1</a> · {pump_recall.alert.title}' in body
    listing = client.get("/work-orders/", **hx("wo-body")).content.decode()
    assert listing.count(">Recall</span>") == 1
    plain = create_work_order(asset=pump, type="repair", priority="normal", problem="Not a recall")
    assert "Recall</span>" not in client.get(f"/work-orders/{plain.number}/", **HX).content.decode()


def test_recall_row_is_plain_text_without_recalls_view_or_a_match(client, signed_in, pump, vent, pump_recall):
    wo = create_work_order(asset=pump, type="recall", priority="high", problem="Inspect", alert=pump_recall.alert)
    signed_in("vendor")  # work orders: Edit, recalls: None
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "<dt>Recall</dt>" in body and "FDA Z-TEST-1 · " in body and "/recalls/?match=" not in body
    signed_in("director")
    unmatched = create_work_order(asset=vent, type="recall", priority="high", problem="Alert without a match for this model", alert=pump_recall.alert)
    body = client.get(f"/work-orders/{unmatched.number}/", **HX).content.decode()
    assert "<dt>Recall</dt>" in body and "/recalls/?match=" not in body


def test_nav_shows_recalls_with_the_needs_action_badge(client, signed_in, pump_recall):
    signed_in("director")
    r = client.get("/equipment/")
    item = next(i for i in r.context["shell"]["nav"] if i["key"] == "recalls")
    assert item["label"] == "Recalls and alerts" and item["url"] == "/recalls/" and item["count"] == 1 and item["hot"]
    assert "Recalls and alerts" in r.content.decode()
    signed_in("requester")  # recalls: None
    assert "recalls" not in [i["key"] for i in client.get("/equipment/").context["shell"]["nav"]]
