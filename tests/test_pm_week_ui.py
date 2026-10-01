"""Auto-assign week on the PM screen (slice 14, views_pm_week): the button only for PM Approve plus work-order assign, the modal's
preview (per technician, nobody credentialed, already held, overdue, nothing to do), the POST (toast, pm-changed, pm-assigned, the modal
closing after settle) and the body that re-fetches on pm-assigned, permissions on GET and POST, and tenant isolation. The service rules
are in test_pm_week.py; these tests pin the PM screen's clock (views_pm._today)."""
import json
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web.views_pm_week import assigned_message
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType
from apps.workorders.services import assign, create_work_order

TODAY = date(2026, 9, 29)  # a Tuesday; the week is Sep 29 through Oct 5
URL = "/pm/week/assign/"
HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {**HX, "HTTP_HX_TARGET": "modal-card"}
BODY = {**HX, "HTTP_HX_TARGET": "pm-body"}
BUTTON = '<button class="btn" type="button" hx-get="/pm/week/assign/" hx-target="#modal-card"'


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr("apps.web.views_pm._today", lambda: TODAY)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def dev(tag, model, dept, day, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=day, status=status)


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Due Sep 30: two ventilators (1.5 h procedure; only Dana) and a pump (1 h; Dana or Tom). Sep 15: a pump, now overdue."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"), checklist=["Inspect"])
    vent_model.save()
    return {"v1": dev("CE-V1", vent_model, dept, date(2026, 9, 30)), "v2": dev("CE-V2", vent_model, dept, date(2026, 9, 30)),
            "p1": dev("CE-P1", pump_model, dept, date(2026, 9, 30)), "p_over": dev("CE-P2", pump_model, dept, date(2026, 9, 15))}


@pytest.fixture
def monitor(ctx, dept):
    """A medium-risk device due Sep 30 that nobody is credentialed for."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
    return dev("CE-M1", dm, dept, date(2026, 9, 30))


def custom_user(tenant, levels, username="custom@riverside.example"):
    role = Role.objects.create(name="Custom", slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def holder(asset):
    w = WorkOrder.objects.filter(asset=asset, type=WoType.PM, status__in=OPEN_STATUSES).first()
    return "-" if w is None else (w.assigned_to.name if w.assigned_to else None)


# --- the button -------------------------------------------------------------------------------------------------------------------

def test_the_button_shows_only_with_pm_approve_and_work_order_assign(client, make_user, tenant, fleet):
    for slug in ("director", "manager"):
        client.force_login(make_user(slug))
        body = client.get("/pm/").content.decode()
        assert BUTTON in body and " Auto-assign week</button>" in body, slug
    for slug in ("technician", "analyst"):
        client.force_login(make_user(slug))
        assert "Auto-assign week" not in client.get("/pm/").content.decode(), slug
    client.force_login(custom_user(tenant, {"pm": Level.APPROVE, "workorders": Level.EDIT}))
    assert "Auto-assign week" not in client.get("/pm/").content.decode()


def test_the_button_sits_with_route_sheets_in_the_page_head(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/pm/").content.decode()
    actions = body[body.index('<div class="actions">'):body.index('id="pm-body"')]
    assert BUTTON in actions and actions.index("Auto-assign week") < actions.index("Route sheets")
    assert '<path d="M20 12a8 8 0 0 1-14.5 4.6' in actions  # the mock's sync icon


# --- permissions ----------------------------------------------------------------------------------------------------------------------

def test_get_and_post_refuse_without_the_permission(client, make_user, tenant, fleet, techs):
    for slug in ("technician", "analyst", "requester", "vendor"):
        client.force_login(make_user(slug))
        assert client.get(URL, **MODAL).status_code == 403, slug
        assert client.post(URL, **MODAL).status_code == 403, slug
    client.force_login(custom_user(tenant, {"pm": Level.APPROVE, "workorders": Level.EDIT}))  # may create a day's PMs, not assign
    assert client.get(URL, **MODAL).status_code == 403 and client.post(URL, **MODAL).status_code == 403
    client.force_login(custom_user(tenant, {"pm": Level.EDIT, "workorders": Level.APPROVE}, username="assigner@riverside.example"))
    assert client.get(URL, **MODAL).status_code == 403 and client.post(URL, **MODAL).status_code == 403
    assert not WorkOrder.objects.exists()


def test_a_custom_role_with_both_levels_may(client, tenant, fleet, techs):
    client.force_login(custom_user(tenant, {"pm": Level.APPROVE, "workorders": Level.APPROVE}))
    assert client.get(URL, **MODAL).status_code == 200
    assert client.post(URL, **MODAL).status_code == 200 and WorkOrder.objects.count() == 3


def test_anonymous_is_sent_to_sign_in_and_other_methods_are_refused(client, signed_in, fleet):
    assert client.get(URL).status_code == 302 and client.post(URL).status_code == 302
    signed_in("director")
    assert client.put(URL).status_code == 405 and client.delete(URL).status_code == 405
    assert not WorkOrder.objects.exists()


# --- the modal ---------------------------------------------------------------------------------------------------------------------

def test_the_modal_previews_the_week(client, signed_in, fleet, techs, monitor):
    signed_in("manager")
    r = client.get(URL, **MODAL)
    assert r.status_code == 200
    body = r.content.decode()
    assert "<h2>Auto-assign week</h2>" in body and "<html" not in body and 'data-act="close-modal"' in body
    assert "Sep 29 to Oct 5, 2026." in body
    assert '<div class="v">3</div><div class="l">PMs to assign</div>' in body and '<div class="v">4</div><div class="l">new work orders</div>' in body
    assert '<div class="v">4.0</div><div class="l">hours assigned</div>' in body
    assert ('<tr><td>Dana Whitfield</td><td class="num">2</td><td class="num">0</td><td class="num">2</td><td class="num">3.0</td></tr>'
            '<tr><td>Tom Okafor</td><td class="num">1</td><td class="num">0</td><td class="num">1</td><td class="num">1.0</td></tr>') in body
    assert ("Nobody is credentialed for 1 device: CE-M1 (Patient monitor, due Sep 30). Its PM stays unassigned; 1 work order is still created "
            "for a manager to assign.") in body
    assert ("Overdue PMs (due before Sep 29) are outside this week and not assigned here. 1 device has no PM work order: open its day on "
            "the calendar (the red counts) to create it.</div>") in body
    assert 'hx-post="/pm/week/assign/" hx-target="#modal-card" hx-disabled-elt="this">' in body and " Assign 3 PMs</button>" in body
    assert not WorkOrder.objects.exists()  # the preview changes nothing


def test_the_modal_counts_open_work_orders_and_held_ones(client, signed_in, fleet, techs):
    assign(create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="PM"), vendor_name="Hamilton Medical")
    create_work_order(asset=fleet["p1"], type=WoType.PM, priority="normal", problem="PM")  # unassigned, like the nightly job's
    signed_in("director")
    body = client.get(URL, **MODAL).content.decode()
    assert '<tr><td>Dana Whitfield</td><td class="num">1</td><td class="num">0</td><td class="num">1</td><td class="num">1.5</td></tr>' in body
    assert '<tr><td>Tom Okafor</td><td class="num">0</td><td class="num">1</td><td class="num">1</td><td class="num">1.0</td></tr>' in body
    assert "1 PM already with a technician or the vendor stays as it is." in body and "Nobody is credentialed" not in body


def test_the_modal_lists_at_most_eight_uncovered_devices(client, signed_in, ctx, dept, monitor):
    for i in range(9):
        dev(f"CE-MX{i}", monitor.device_model, dept, date(2026, 10, 1))
    signed_in("director")
    body = client.get(URL, **MODAL).content.decode()
    assert "Nobody is credentialed for 10 devices: CE-M1 (Patient monitor, due Sep 30), CE-MX0" in body and ", and 2 more. Their PMs stay unassigned" in body
    assert "10 work orders are still created for a manager to assign." in body and " Create 10 PM work orders</button>" in body
    assert "CE-MX7" not in body and "<table>" not in body


def test_with_nothing_to_do_the_modal_says_so_and_offers_no_button(client, signed_in, ctx, techs):
    signed_in("director")
    body = client.get(URL, **MODAL).content.decode()
    assert "Nothing to assign: no PMs are due this week." in body and "hx-post" not in body and ">Close</button>" in body


def test_after_assigning_the_modal_has_nothing_to_do(client, signed_in, fleet, techs, monitor):
    signed_in("director")
    client.post(URL, **MODAL)
    body = client.get(URL, **MODAL).content.decode()
    assert "Nothing to assign: the PMs this week that are on nobody's plate need a credentialed technician." in body and "hx-post" not in body
    assert "3 PMs already with a technician or the vendor stay as they are." in body and "Nobody is credentialed for 1 device: CE-M1" in body
    assert "PMs to assign" not in body and "<table>" not in body


# --- the POST ------------------------------------------------------------------------------------------------------------------------

def test_post_assigns_toasts_refreshes_and_closes_the_modal(client, signed_in, fleet, techs, monitor):
    signed_in("manager")
    r = client.post(URL, **MODAL)
    assert r.status_code == 200 and r.content == b""
    now = triggers(r)
    assert now["toast"]["value"] == ("Auto-assign balanced 3 PMs across 2 technicians by credential and workload; 1 left unassigned: "
                                     "nobody is credentialed for it")
    assert "pm-changed" in now and "pm-assigned" in now and "modal-close" not in now
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert {t: holder(fleet[t]) for t in ("v1", "v2", "p1", "p_over")} == {"v1": "Dana Whitfield", "v2": "Dana Whitfield", "p1": "Tom Okafor", "p_over": "-"}
    assert holder(monitor) is None


def test_the_body_refetches_on_pm_assigned_and_names_the_new_assignees(client, signed_in, fleet, techs, monitor):
    signed_in("director")
    page = client.get("/pm/?y=2026&m=9&day=2026-09-30").content.decode()
    span = ('<span hidden hx-get="/pm/?y=2026&amp;m=9&amp;day=2026-09-30" '
            'hx-trigger="devices-changed from:body, pm-assigned from:body, models-changed from:body" hx-target="#pm-body"')
    assert span in page
    client.post(URL, **MODAL)
    body = client.get("/pm/?y=2026&m=9&day=2026-09-30", **BODY).content.decode()  # what the span fetches
    assert body.count('<span class="chip info" title="Dana Whitfield">PM open, Dana</span>') == 2 and "PM open, Tom" in body
    assert body.count("PM open, unassigned") == 1 and "Every PM on this day already has an open work order" in body


def test_create_for_a_day_does_not_fire_pm_assigned(client, signed_in, fleet, techs):
    """Create answers with the body itself; firing pm-assigned too would fetch it twice."""
    signed_in("director")
    r = client.post("/pm/day/2026-09-30/work-orders/?y=2026&m=9", **BODY)
    assert "pm-assigned" not in triggers(r) and "pm-assigned" not in triggers(r, "HX-Trigger-After-Settle")


def test_a_second_post_is_a_no_op(client, signed_in, fleet, techs, monitor):
    signed_in("director")
    client.post(URL, **MODAL)
    count = WorkOrder.objects.count()
    r = client.post(URL, **MODAL)
    assert r.status_code == 200 and WorkOrder.objects.count() == count
    now = triggers(r)
    assert now["toast"]["value"] == "Nothing to assign: 1 PM this week still needs a credentialed technician"
    assert "pm-changed" not in now and "pm-assigned" not in now and "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_post_without_technicians_creates_unassigned(client, signed_in, fleet):
    signed_in("director")
    r = client.post(URL, **MODAL)
    assert triggers(r)["toast"]["value"] == "3 PM work orders created, none assigned: nobody is credentialed for these devices"
    assert "pm-changed" in triggers(r) and not WorkOrder.objects.filter(assigned_to__isnull=False).exists()


@pytest.mark.parametrize("kw, expected", [
    ({"assigned": 1, "technicians": 1}, "Auto-assign balanced 1 PM across 1 technician by credential and workload"),
    ({"assigned": 23, "technicians": 4, "unassigned": 2}, "Auto-assign balanced 23 PMs across 4 technicians by credential and workload; "
                                                        "2 left unassigned: nobody is credentialed for them"),
    ({"created": 1, "unassigned": 1}, "1 PM work order created, none assigned: nobody is credentialed for this device"),
    ({"unassigned": 2}, "Nothing to assign: 2 PMs this week still need a credentialed technician"),
    ({"held": 5}, "Nothing to assign: every PM due this week is already with a technician or the vendor"),
    ({}, "Nothing to assign: no PMs are due this week"),
])
def test_toast_wording(kw, expected):
    class Done:
        assigned = technicians = created = unassigned = held = 0

    done = Done()
    for k, v in kw.items():
        setattr(done, k, v)
    assert assigned_message(done) == expected


# --- tenant isolation ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def theirs(other_tenant):
    """Another hospital: a pump due Sep 30 with a credentialed technician, and one due Oct 1 with an unassigned open PM."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Their pump", category="Infusion pumps",
                                        risk_class=RiskClass.HIGH)
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, next_pm_on=date(2026, 9, 30))
        b = Asset.objects.create(tag="THEIRS-2", device_model=dm, department=d, next_pm_on=date(2026, 10, 1))
        wo = create_work_order(asset=b, type=WoType.PM, priority="normal", problem="PM")
        tech = Technician.objects.create(name="Aaron Theirs")
        Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Infusion pumps")
    return {"a": a, "b": b, "wo": wo, "tech": tech}


def test_another_facility_is_never_shown_or_touched(client, signed_in, fleet, techs, theirs, other_tenant):
    signed_in("director")
    body = client.get(URL, **MODAL).content.decode()
    assert "THEIRS" not in body and "Aaron" not in body and " Assign 3 PMs</button>" in body
    r = client.post(URL, **MODAL)
    assert triggers(r)["toast"]["value"] == "Auto-assign balanced 3 PMs across 2 technicians by credential and workload"
    with tenant_context(other_tenant):
        assert not WorkOrder.objects.filter(asset=theirs["a"]).exists()
        theirs["wo"].refresh_from_db()
        assert theirs["wo"].assigned_to is None


def test_another_facilitys_user_assigns_only_their_own(client, make_user, fleet, techs, theirs, other_tenant):
    client.force_login(make_user("director", tenant_=other_tenant, username="dir@other.example"))
    body = client.get(URL, **MODAL).content.decode()
    assert "Aaron Theirs" in body and "Dana" not in body and " Assign 2 PMs</button>" in body
    r = client.post(URL, **MODAL)
    assert triggers(r)["toast"]["value"] == "Auto-assign balanced 2 PMs across 1 technician by credential and workload"
    assert not WorkOrder.objects.exists()  # ours: still nothing
    with tenant_context(other_tenant):
        assert {w.asset.tag: w.assigned_to.name for w in WorkOrder.objects.all()} == {"THEIRS-1": "Aaron Theirs", "THEIRS-2": "Aaron Theirs"}


def test_overdue_pms_already_open_on_nobodys_plate_point_to_work_orders(client, signed_in, fleet):
    """The nightly job opened the overdue pump's PM before it fell due; Create on its day does nothing, so the modal says where to
    assign it instead of sending the user to the calendar."""
    from apps.pm.services import generate_pm_work_orders

    generate_pm_work_orders(as_of=date(2026, 9, 1), lead_days=21)
    signed_in("director")
    body = client.get(URL, **MODAL).content.decode()
    assert ("1 open PM work order is on nobody's plate: assign it from "
            '<a href="/work-orders/?type=pm&amp;assigned=unassigned">Work orders</a>.') in body
    assert "has no PM work order" not in body
