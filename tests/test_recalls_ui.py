"""Recalls and alerts screen (slice 6): the page and its pills, cards by status, server-side permission checks on every
action, tenant isolation, the expanded device table, and the API actions that mirror the screen."""
from datetime import date, timedelta

import pytest

from apps.accounts.models import Level, Role, create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.recalls import services as rc
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.workorders import services as wo_services
from apps.workorders.models import WorkOrder, WoStatus

HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "rc-body"}
S = AlertMatch.Status
TODAY = date.today()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def pumps(ctx, dept, pump_model, pump):
    """Three active pumps (ICU 2, ED 1) and one retired."""
    ed = Department.objects.create(name="ED")
    Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, serial="BD123")
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=ed, room="4")
    Asset.objects.create(tag="CE-10005", device_model=pump_model, department=ed, status="retired")


@pytest.fixture
def theirs(tenant, other_tenant, pump_recall):
    """The same global alert matched to the other hospital's catalog."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"))
        return AlertMatch.objects.create(alert=pump_recall.alert, device_model=dm)


@pytest.fixture
def reviewer(ctx, tenant):
    """A custom role that may review recalls and create the work orders but not close or reopen them."""
    role = Role.objects.create(name="Recall reviewer", slug="reviewer")
    role.set_levels({"recalls": Level.EDIT, "equipment": Level.VIEW, "workorders": Level.VIEW})
    return role


def status_url(m, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/recalls/{m.pk}/status/" + (f"?{q}" if q else "")


def wo_url(match):
    return f"/recalls/{match.pk}/work-orders/"


# --- page ---------------------------------------------------------------------------------------------

def test_page_renders_head_pills_and_a_needs_action_card(client, signed_in, pump_recall, pumps):
    signed_in("director")
    r = client.get("/recalls/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Recalls and alerts" in body and "Alerts are matched to the inventory by manufacturer and model" in body
    assert f"newest FDA notice imported {TODAY:%b} {TODAY.day}, {TODAY.year}" in body and "ECRI alerts need a licensed feed (not connected)" in body
    assert "Match alerts to inventory" in body and "Check feeds" not in body
    assert 'href="/print/reports/recall/"' in body  # Response log (slice 11) prints the recall report
    assert "All alerts · 1" in body and "Needs action or review · 1" in body and "In progress · 0" in body and "Closed · 0" in body
    assert '<span class="src">FDA</span><span>FDA Z-TEST-1</span>' in body and "Class II" in body
    assert "<h3>Keypad membrane may allow fluid ingress</h3>" in body and "BD Alaris 8015 PCU" in body
    assert "<b>3</b> devices affected · ICU 2, ED 1" in body and '<span class="chip crit">Needs action</span>' in body
    assert "Create 3 work orders" in body and "Mark under review" in body and "Show affected devices" in body
    assert f'id="match-{pump_recall.pk}"' in body and "class=\"alert\"" in body  # Class II: no crit rail
    assert 'hx-confirm="Create 3 work orders for BD Alaris 8015 PCU' in body


def test_pills_filter_the_body_partial_and_empty_state(client, signed_in, pump_recall, pumps):
    signed_in("analyst")
    partial = client.get("/recalls/?view=action", **BODY)
    assert partial.status_code == 200 and b"<html" not in partial.content and b'id="rc-body"' in partial.content
    assert b"FDA Z-TEST-1" in partial.content and b'class="pill active"' in partial.content
    empty = client.get("/recalls/?view=closed", **BODY).content.decode()
    assert "No alerts in this view." in empty and "FDA Z-TEST-1" not in empty
    assert "?view=closed" in empty and 'aria-current="true"' in empty
    assert client.get("/recalls/?view=bogus").context["view"] == "all"
    assert b'hx-trigger="recalls-changed from:body"' in partial.content


def test_no_alerts_page(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/recalls/").content.decode()
    assert "newest FDA notice imported never" in body and "No alerts in this view." in body and "All alerts · 0" in body


def test_class_one_and_closed_cards(client, signed_in, pump_recall, pumps, vent_model, vent):
    signed_in("director")
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-CRIT", classification="Class I", manufacturer="Hamilton Medical",
                                 product="Ventilator", title="Software may transition to standby", action="Apply the update.", published_on=TODAY)
    crit = AlertMatch.objects.create(alert=alert, device_model=vent_model)
    rc.set_status(pump_recall, S.NOT_AFFECTED, note="Serial range not in our fleet.")
    body = client.get("/recalls/").content.decode()
    assert 'class="alert crit"' in body and 'id="match-%s"' % crit.pk in body and '<div class="hint action">Apply the update.</div>' in body
    assert 'class="alert quiet"' in body and "Serial range not in our fleet." in body and "Reopen" in body
    assert '<span class="chip neutral">Reviewed, not affected</span>' in body and "Close, no action needed" not in body
    assert "Closed · 1" in body and "Needs action or review · 1" in body


# --- permissions --------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["requester", "vendor"])
def test_roles_without_access_get_403(client, signed_in, pump_recall, pumps, role):
    signed_in(role)
    assert client.get("/recalls/").status_code == 403
    assert client.post(status_url(pump_recall), {"to": S.UNDER_REVIEW}).status_code == 403
    assert client.post(wo_url(pump_recall)).status_code == 403
    assert client.post("/recalls/match/").status_code == 403
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.NEEDS_ACTION


@pytest.mark.parametrize("role", ["technician", "analyst"])
def test_view_only_roles_see_no_buttons_and_cannot_post(client, signed_in, pump_recall, pumps, role):
    signed_in(role)
    body = client.get("/recalls/").content.decode()
    assert "FDA Z-TEST-1" in body and "hx-post" not in body and "Match alerts to inventory" not in body
    assert "Show affected devices" in body  # reading is fine
    for to in (S.UNDER_REVIEW, S.IN_PROGRESS, S.NOT_AFFECTED):
        assert client.post(status_url(pump_recall), {"to": to}, **HX).status_code == 403
    assert client.post(wo_url(pump_recall), **HX).status_code == 403
    assert client.post("/recalls/match/", **HX).status_code == 403
    assert client.get(status_url(pump_recall)).status_code == 405
    assert not WorkOrder.objects.exists()


def test_manager_can_review_create_work_orders_close_and_reopen(client, signed_in, pump_recall, pumps, techs):
    signed_in("manager")
    r = client.post(status_url(pump_recall), {"to": S.UNDER_REVIEW}, **HX)
    assert r.status_code == 200 and "FDA Z-TEST-1: under review" in r["HX-Trigger"]
    body = r.content.decode()
    assert b"<html" not in r.content and 'id="rc-body"' in body and '<span class="chip warn">Under review</span>' in body
    assert "Create 3 work orders" in body and "Close, no action needed" in body and "Mark under review" not in body

    r = client.post(wo_url(pump_recall), **HX)
    assert r.status_code == 200 and "3 recall work orders created and assigned to credentialed technicians" in r["HX-Trigger"]
    body = r.content.decode()
    assert '<div class="bar"><i style="width:0.0%"></i></div>' in body and "0 of 3 devices completed" in body
    assert 'href="/work-orders/?type=recall&amp;q=Z-TEST-1&amp;open=0&amp;open=1"' in body and ">Open work orders</a>" in body
    assert ">Close</button>" in body and "Create 3 work orders" not in body and "In progress · 1" in body
    assert WorkOrder.objects.filter(alert=pump_recall.alert, assigned_to=techs["dana"]).count() == 3

    r = client.post(wo_url(pump_recall), **HX)
    assert "Every affected device already has a recall work order" in r["HX-Trigger"]

    r = client.post(status_url(pump_recall), {"to": S.CLOSED}, **HX)
    assert r.status_code == 200 and "FDA Z-TEST-1: closed" in r["HX-Trigger"]
    body = r.content.decode()
    pump_recall.refresh_from_db()
    assert pump_recall.closed_on == TODAY and f"Closed {rc.fmt_date(TODAY)} by Manager User" in body and 'class="alert quiet"' in body
    assert ">Reopen</button>" in body and "hx-confirm" not in body.split(">Reopen</button>")[0][-200:] and "Closed · 1" in body

    r = client.post(status_url(pump_recall), {"to": S.UNDER_REVIEW}, **HX)
    assert "FDA Z-TEST-1: under review" in r["HX-Trigger"]
    pump_recall.refresh_from_db()
    assert pump_recall.closed_on is None and pump_recall.disposition_note.startswith("Closed ")
    # the work orders exist, so the device table still lists the devices and the batch would skip them
    r = client.post(status_url(pump_recall), {"to": S.NOT_AFFECTED}, **HX)
    assert "FDA Z-TEST-1: reviewed, not affected" in r["HX-Trigger"] and 'class="alert quiet"' in r.content.decode()


def test_a_disallowed_move_toasts_the_rule_and_changes_nothing(client, signed_in, pump_recall, pumps):
    signed_in("manager")
    r = client.post(status_url(pump_recall), {"to": S.CLOSED}, **HX)
    assert r.status_code == 200 and "Cannot move FDA Z-TEST-1 from needs action to closed." in r["HX-Trigger"]
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.NEEDS_ACTION


def test_unassigned_toast_when_nobody_is_credentialed(client, signed_in, pump_recall, pumps):
    signed_in("manager")
    r = client.post(wo_url(pump_recall), **HX)
    assert "3 recall work orders created; no credentialed technician, left unassigned" in r["HX-Trigger"]
    assert WorkOrder.objects.filter(alert=pump_recall.alert, assigned_to=None).count() == 3


def test_work_orders_for_a_match_without_devices_toasts_the_rule(client, signed_in, pump_recall):
    signed_in("manager")
    r = client.post(wo_url(pump_recall), **HX)
    assert r.status_code == 200 and "No active devices match FDA Z-TEST-1." in r["HX-Trigger"]
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.NEEDS_ACTION and b"Create 0 work orders" not in client.get("/recalls/").content


def test_edit_only_role_can_review_and_create_but_not_close_or_reopen(client, signed_in, pump_recall, pumps, reviewer):
    signed_in("reviewer")
    body = client.get("/recalls/").content.decode()
    assert "Mark under review" in body and "Create 3 work orders" in body and "Match alerts to inventory" in body
    assert client.post(status_url(pump_recall), {"to": S.NOT_AFFECTED}, **HX).status_code == 403
    r = client.post(status_url(pump_recall), {"to": S.UNDER_REVIEW}, **HX)
    assert r.status_code == 200 and "Create 3 work orders" in r.content.decode() and "Close, no action needed" not in r.content.decode()
    assert client.post(status_url(pump_recall), {"to": S.NOT_AFFECTED}, **HX).status_code == 403
    r = client.post(wo_url(pump_recall), **HX)
    assert r.status_code == 200 and "3 recall work orders created" in r["HX-Trigger"] and ">Close</button>" not in r.content.decode()
    assert "Open work orders" in r.content.decode()
    assert client.post(status_url(pump_recall), {"to": S.CLOSED}, **HX).status_code == 403
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.IN_PROGRESS
    rc.set_status(pump_recall, S.CLOSED)
    assert ">Reopen</button>" not in client.get("/recalls/").content.decode()
    assert client.post(status_url(pump_recall), {"to": S.UNDER_REVIEW}, **HX).status_code == 403
    assert client.post("/recalls/match/", **HX).status_code == 200


# --- tenant isolation ----------------------------------------------------------------------------------

def test_other_tenants_match_is_absent_and_404s(client, signed_in, pump_recall, pumps, theirs):
    signed_in("director")
    body = client.get(f"/recalls/?match={theirs.pk}").content.decode()
    assert "All alerts · 1" in body and f'id="match-{theirs.pk}"' not in body and "THEIRS-1" not in body and "<b>3</b> devices" in body
    assert client.post(status_url(theirs), {"to": S.UNDER_REVIEW}, **HX).status_code == 404
    assert client.post(wo_url(theirs), **HX).status_code == 404
    with tenant_context(theirs.tenant):
        theirs.refresh_from_db()
    assert theirs.status == S.NEEDS_ACTION and not WorkOrder.objects.exists()


# --- expanded card --------------------------------------------------------------------------------------

def test_match_query_expands_the_device_table_and_scrolls(client, signed_in, pump_recall, pumps, pump_model, dept):
    signed_in("director")
    closed = client.get("/recalls/").content.decode()
    assert "<table" not in closed and "Show affected devices" in closed and f"?match={pump_recall.pk}" in closed
    r = client.get(f"/recalls/?match={pump_recall.pk}")
    body = r.content.decode()
    assert 'class="alert exp"' in body and "Hide affected devices" in body and f'getElementById("match-{pump_recall.pk}")' in body
    assert "<th>Asset tag</th><th>Location</th><th>Serial</th><th>Status</th><th>Next PM</th>" in body
    assert 'href="/equipment/CE-10003/"' in body and "BD123" in body and "<td>ED</td>" in body and "CE-10005" not in body
    assert '<span class="chip ok">In service</span>' in body and f"Due {TODAY + timedelta(days=90):%b %Y}" in body and "See all" not in body
    for i in range(8):
        Asset.objects.create(tag=f"CE-2000{i}", device_model=pump_model, department=dept)
    body = client.get(f"/recalls/?view=action&match={pump_recall.pk}", **BODY).content.decode()
    assert body.count('<tr class="row"') == 8 and 'href="/equipment/?q=Alaris+8015+PCU">See all 11 in Equipment</a>' in body
    assert "Create 11 work orders" in body
    assert f"?view=action&amp;match={pump_recall.pk}" not in body.split('class="pills')[1].split("</div>")[0]  # pills drop the match


def test_posts_keep_the_view_and_expanded_card(client, signed_in, pump_recall, pumps):
    signed_in("manager")
    r = client.post(status_url(pump_recall, view="action", match=pump_recall.pk), {"to": S.UNDER_REVIEW}, **HX)
    body = r.content.decode()
    assert 'class="alert exp"' in body and "<table" in body and "Hide affected devices" in body
    assert f'hx-get="/recalls/?view=action&amp;match={pump_recall.pk}"' in body  # the body re-fetches the same view
    assert f'hx-post="/recalls/{pump_recall.pk}/work-orders/?view=action&amp;match={pump_recall.pk}"' in body


def test_in_progress_card_shows_the_bar_and_the_work_orders_link(client, signed_in, pump_recall, pumps, techs):
    signed_in("analyst")
    rc.create_recall_work_orders(pump_recall)
    wo = WorkOrder.objects.filter(alert=pump_recall.alert).first()
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    wo_services.change_status(wo, WoStatus.COMPLETED)
    body = client.get("/recalls/?view=progress").content.decode()
    assert '<i style="width:33.3%"></i>' in body and "1 of 3 devices completed" in body and ">Open work orders</a>" in body
    assert '<span class="chip info">Action in progress</span>' in body and "hx-post" not in body


def test_match_button_finds_new_device_models(client, signed_in, pump_recall):
    signed_in("director")
    r = client.post("/recalls/match/?view=all", **HX)
    assert r.status_code == 200 and "No new matches" in r["HX-Trigger"] and b'id="rc-body"' in r.content
    pump_recall.alert.model_terms = ["Alaris"]
    pump_recall.alert.save()
    DeviceModel.objects.create(manufacturer="BD", model="Alaris 8100", description="Pump module", category="Infusion pumps")
    r = client.post("/recalls/match/", **HX)
    assert "1 new match" in r["HX-Trigger"] and "All alerts · 2" in r.content.decode() and "BD Alaris 8100" in r.content.decode()


# --- API ------------------------------------------------------------------------------------------------

def api(match, action=""):
    return f"/api/v1/alert-matches/{match.id}/{action}"


def test_api_transition_and_work_orders_mirror_the_screen(client, signed_in, pump_recall, pumps, techs):
    signed_in("manager")
    r = client.get(api(pump_recall))
    assert r.status_code == 200 and r.json()["progress"] == {"total": 3, "completed": 0} and r.json()["affected_count"] == 3
    r = client.post(api(pump_recall, "transition/"), {"status": S.UNDER_REVIEW})
    assert r.status_code == 200 and r.json()["status"] == S.UNDER_REVIEW
    r = client.post(api(pump_recall, "transition/"), {"status": S.CLOSED})
    assert r.status_code == 400 and "Cannot move FDA Z-TEST-1" in r.json()["detail"]
    r = client.post(api(pump_recall, "work-orders/"))
    assert r.status_code == 200 and r.json()["created"] == 3 and r.json()["status"] == S.IN_PROGRESS and r.json()["progress"] == {"total": 3, "completed": 0}
    assert client.post(api(pump_recall, "work-orders/")).json()["created"] == 0
    r = client.post(api(pump_recall, "transition/"), {"status": S.CLOSED, "note": "All units updated."})
    assert r.status_code == 200 and r.json()["disposition_note"] == "All units updated." and r.json()["closed_on"] == TODAY.isoformat()
    assert client.post("/api/v1/alert-matches/", {"alert": str(pump_recall.alert_id), "device_model": str(pump_recall.device_model_id)}).status_code == 405


def test_api_patch_rejects_status_changes(client, signed_in, pump_recall):
    signed_in("manager")
    r = client.patch(api(pump_recall), {"status": S.CLOSED}, content_type="application/json")
    assert r.status_code == 400 and "transition" in str(r.json()["status"])
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.NEEDS_ACTION
    ok = client.patch(api(pump_recall), {"disposition_note": "Awaiting the vendor letter", "status": S.NEEDS_ACTION}, content_type="application/json")
    assert ok.status_code == 200 and ok.json()["disposition_note"] == "Awaiting the vendor letter"


def test_api_levels_match_the_screen(client, signed_in, pump_recall, pumps, reviewer):
    signed_in("technician")
    assert client.get(api(pump_recall)).status_code == 200
    assert client.post(api(pump_recall, "transition/"), {"status": S.UNDER_REVIEW}).status_code == 403
    assert client.post(api(pump_recall, "work-orders/")).status_code == 403
    client.logout()
    signed_in("reviewer")
    assert client.patch(api(pump_recall), {"disposition_note": "x"}, content_type="application/json").status_code == 403
    assert client.post(api(pump_recall, "transition/"), {"status": S.NOT_AFFECTED}).status_code == 403
    assert client.post(api(pump_recall, "transition/"), {"status": S.UNDER_REVIEW}).status_code == 200
    assert client.post(api(pump_recall, "work-orders/")).status_code == 200
    assert client.post(api(pump_recall, "transition/"), {"status": S.CLOSED}).status_code == 403
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.IN_PROGRESS


def test_api_other_tenants_match_404s(client, signed_in, pump_recall, theirs):
    signed_in("manager")
    assert client.get(api(theirs)).status_code == 404
    assert client.post(api(theirs, "transition/"), {"status": S.UNDER_REVIEW}).status_code == 404
    assert [m["id"] for m in client.get("/api/v1/alert-matches/").json()["results"]] == [str(pump_recall.id)]


# --- review follow-ups ---------------------------------------------------------------------------------

def test_roles_without_recalls_access_get_403_from_the_api_too(client, signed_in, pump_recall, pumps):
    for slug in ("requester", "vendor"):
        signed_in(slug)
        assert client.get("/api/v1/alert-matches/").status_code == 403, slug
        assert client.get(api(pump_recall)).status_code == 403, slug
        assert client.post(api(pump_recall, "transition/"), {"status": "under_review"}).status_code == 403, slug
        assert client.post(api(pump_recall, "work-orders/")).status_code == 403, slug
        assert client.patch(api(pump_recall), {"disposition_note": "x"}, content_type="application/json").status_code == 403, slug
    client.logout()
    assert client.get("/api/v1/alert-matches/").status_code in (401, 403)
    assert client.post(api(pump_recall, "work-orders/")).status_code in (401, 403)


def test_api_error_paths(client, signed_in, pump_recall, pumps, ctx, vent_model):
    signed_in("manager")
    r = client.post(api(pump_recall, "transition/"), {}, content_type="application/json")
    assert r.status_code == 400 and "Required" in str(r.json())
    r = client.post(api(pump_recall, "transition/"), {"status": "bogus"}, content_type="application/json")
    assert r.status_code == 400
    r = client.post(api(pump_recall, "transition/"), {"status": "closed"}, content_type="application/json")
    assert r.status_code == 400 and "Cannot move FDA Z-TEST-1" in r.json()["detail"]
    empty = AlertMatch.objects.create(alert=pump_recall.alert, device_model=vent_model)
    r = client.post(api(empty, "work-orders/"))
    assert r.status_code == 400 and "No active devices" in r.json()["detail"]


def test_api_patch_cannot_repoint_a_match_or_set_closed_on(client, signed_in, pump_recall, ctx, vent_model):
    signed_in("manager")
    other = AlertMatch.objects.create(alert=pump_recall.alert, device_model=vent_model)
    payload = {"device_model": str(vent_model.id), "closed_on": "2020-01-01", "disposition_note": "noted"}
    r = client.patch(api(pump_recall), payload, content_type="application/json")
    pump_recall.refresh_from_db()
    assert r.status_code == 200 and pump_recall.device_model_id != vent_model.id and pump_recall.closed_on is None and pump_recall.disposition_note == "noted"
    assert AlertMatch.objects.filter(device_model=vent_model).count() == 1 and other.pk != pump_recall.pk


def test_head_button_keeps_the_pages_view_and_expanded_card(client, signed_in, pump_recall, pumps):
    signed_in("manager")
    r = client.post("/recalls/match/", **HX, HTTP_HX_CURRENT_URL=f"http://testserver/recalls/?view=action&match={pump_recall.pk}")
    body = r.content.decode()
    assert r.status_code == 200 and 'aria-current="true">Needs action or review' in body and f'id="match-{pump_recall.pk}"' in body and "alert exp" in body


def test_recalls_needs_sign_in(client, db):
    assert client.get("/recalls/").status_code == 302 and client.get("/recalls/")["Location"].startswith("/login/")
