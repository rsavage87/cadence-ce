"""A work order's labor, parts, and notes over the API (slice 19, part A; apps/api/views_work.py): the drawer's Log time, Add part,
Remove, and Add a note through the same services (apps.workorders.costs, add_note) with the same levels (apps/workorders/permissions.py),
by session and by token. A scoped user (apps.workorders.scoping) reaches them for the work orders in their share only, and reads other
work orders' numbers in the timeline as the drawer shows them."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Module, Role, User, create_default_roles
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.workorders import costs
from apps.workorders.models import LaborLine, PartLine, WorkOrderNote, WoStatus
from apps.workorders.services import add_note, assign, change_status, create_work_order, timeline

API = "/api/v1/work-orders/"
HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
OPENED = TODAY - timedelta(days=5)
# role, may record work and note (work orders Edit), may set a rate (work orders Approve)
ROLES = [("director", True, True), ("manager", True, True), ("technician", True, False), ("vendor", True, False), ("analyst", False, False),
         ("requester", False, False)]
LABOR = {"worked_on": TODAY.isoformat(), "hours": "1.5", "description": "Replaced latch"}
PART = {"description": "Pump door latch", "part_number": "10013-B", "quantity": "2", "unit_cost": "42", "po_number": "PO-5566"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, company="", department=""):
        user = make_user(role_slug)
        if company or department:
            user.company, user.department = company, department
            user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def wo(ctx, pump, techs):
    return create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch broken", opened_on=OPENED, assigned_to=techs["dana"])


@pytest.fixture
def vendor_wo(ctx, vent):
    return create_work_order(asset=vent, type="repair", priority="high", problem="Flow sensor fault", opened_on=OPENED, vendor_service=True,
                             vendor_name="Hamilton Medical field service")


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's work order (opened in 2025, so its number is not one of ours), technician, and lines."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, next_pm_on=TODAY + timedelta(days=30))
        tech = Technician.objects.create(name="Their Tech")
        their_wo = create_work_order(asset=asset, type="repair", priority="normal", problem="Theirs", opened_on=date(2025, 6, 1), assigned_to=tech)
        labor = costs.add_labor(their_wo, hours="1", worked_on=TODAY, by=None)
        part = costs.add_part(their_wo, description="Their part", quantity="1", unit_cost="10", by=None)
    return {"wo": their_wo, "tech": tech, "labor": labor, "part": part}


def url(wo, what, line=None):
    return f"{API}{wo.pk}/{what}/" + (f"{line.pk}/" if line is not None else "")


def post(client, address, body, **extra):
    return client.post(address, body, content_type="application/json", **extra)


def share(user, wo):
    """A clinical requester sees their unit's work orders and a vendor technician their company's (slice 16): give them this one."""
    user.department, user.company = "ICU", "BD"
    user.save()
    if user.role.slug == "vendor":
        assign(wo, vendor_name="BD field service")


def token(make_user, slug, **attrs):
    user = make_user(slug)
    for k, v in attrs.items():
        setattr(user, k, v)
    user.save()
    return {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


# --- labor -----------------------------------------------------------------------------------------------------------------------

def test_log_time_goes_through_the_service(client, signed_in, wo, techs):
    user = signed_in("technician")
    r = post(client, url(wo, "labor"), LABOR)
    assert r.status_code == 201, r.content
    line = LaborLine.objects.get()
    assert r.json() == {"id": str(line.pk), "technician": str(techs["dana"].pk), "who": "Dana Whitfield", "worked_on": TODAY.isoformat(),
                        "hours": "1.50", "rate": "82.00", "amount": "123.00", "description": "Replaced latch",
                        "created_at": r.json()["created_at"]}
    assert line.work_order == wo and line.tenant_id == wo.tenant_id  # the assigned technician and the Settings rate, as the drawer charges
    h = LaborLine.history.get(id=line.pk)
    assert (h.history_type, h.history_change_reason, h.history_user) == ("+", costs.CHANGE_ADDED, user)
    assert timeline(wo)[-1]["text"] == "1.5 h logged by Dana Whitfield"


def test_the_api_and_the_drawer_record_the_same_line(client, signed_in, wo, techs):
    """The same Log time through the drawer and through the API: the same line, the same price, the same timeline entry."""
    signed_in("technician")
    body = {**LABOR, "technician": str(techs["tom"].pk), "hours": "2.25"}
    assert client.post(f"/work-orders/{wo.number}/labor/", body, **HX).status_code == 200
    assert post(client, url(wo, "labor"), body).status_code == 201
    web, api = LaborLine.objects.order_by("created_at")
    fields = ("technician_id", "worked_on", "hours", "rate", "description")
    assert [getattr(web, f) for f in fields] == [getattr(api, f) for f in fields] and costs.labor_amount(web) == costs.labor_amount(api)
    assert [e["text"] for e in timeline(wo)][-2:] == ["2.25 h logged by Tom Okafor"] * 2


def test_the_labor_list_is_the_drawers(client, signed_in, wo, techs):
    later = costs.add_labor(wo, hours="0.25", worked_on=TODAY, technician=techs["tom"], rate="82.50", by=None)
    first = costs.add_labor(wo, hours="0.25", worked_on=TODAY - timedelta(days=1), rate="82.50", by=None)
    signed_in("analyst")
    data = client.get(url(wo, "labor")).json()
    assert [line["id"] for line in data["results"]] == [str(first.pk), str(later.pk)]  # by the day worked, as the drawer
    assert [line["amount"] for line in data["results"]] == ["20.63", "20.63"]  # 0.25 h × $82.50 = $20.625, each line to the cent, half up
    assert (data["count"], data["hours"], data["total"], data["settings_rate"]) == (2, "0.50", "41.26", "82.00")
    assert (data["work_order"], data["can_record"], data["can_set_rate"], data["locked"]) == (wo.number, False, False, "")
    assert data["results"][0]["who"] == "Dana Whitfield"


def test_vendor_service_time_has_no_technician_and_the_vendor_rate(client, signed_in, vendor_wo, techs):
    signed_in("technician")
    r = post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "2"})
    assert r.status_code == 201 and (r.json()["technician"], r.json()["who"], r.json()["rate"], r.json()["amount"]) == \
        (None, "Hamilton Medical field service", "215.00", "430.00")
    r = post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "2", "technician": str(techs["dana"].pk)})
    assert r.status_code == 400 and r.json() == {"technician": ["Vendor service time is logged without a technician."]}
    assert client.get(url(vendor_wo, "labor")).json()["settings_rate"] == "215.00"


def test_a_different_rate_needs_approve(client, signed_in, wo):
    signed_in("technician")
    for rate in ("95", "abc", 95):
        r = post(client, url(wo, "labor"), {**LABOR, "rate": rate})
        assert r.status_code == 403 and "needs work-order Approve" in r.json()["detail"], rate
    assert not LaborLine.objects.exists()
    for rate in ("$82.00", "82", 82, "", None):  # the Settings rate, typed out, or none: anyone's
        assert post(client, url(wo, "labor"), {**LABOR, "hours": "1", "rate": rate}).status_code == 201, rate
    assert set(LaborLine.objects.values_list("rate", flat=True)) == {Decimal("82.00")}
    signed_in("manager")
    r = post(client, url(wo, "labor"), {**LABOR, "rate": "$1,250.00"})
    assert r.status_code == 201 and r.json()["rate"] == "1250.00" and r.json()["amount"] == "1875.00"
    assert post(client, url(wo, "labor"), {**LABOR, "rate": "abc"}).json() == {"rate": ["Enter a number."]}
    assert client.get(url(wo, "labor")).json()["can_set_rate"] is True


def test_a_new_settings_rate_prices_time_logged_afterwards(client, signed_in, wo):
    signed_in("technician")
    post(client, url(wo, "labor"), LABOR)
    fs.update_settings(labor_rate=Decimal("90"))
    post(client, url(wo, "labor"), {**LABOR, "rate": "90"})  # the new Settings rate, typed out
    assert list(LaborLine.objects.order_by("created_at").values_list("rate", flat=True)) == [Decimal("82.00"), Decimal("90.00")]


@pytest.mark.parametrize("change, errors", [
    ({"hours": "0"}, {"hours": "Log more than 0 and at most 24 hours on one line."}),
    ({"hours": "24.5"}, {"hours": "Log more than 0 and at most 24 hours on one line."}),
    ({"hours": "1.255"}, {"hours": "Use at most 2 decimal places."}),
    ({"hours": 1.255}, {"hours": "Use at most 2 decimal places."}),
    ({"hours": ""}, {"hours": "Enter the hours worked."}),
    ({"hours": None}, {"hours": "Enter the hours worked."}),
    ({"worked_on": (TODAY + timedelta(days=1)).isoformat()}, {"worked_on": "The date cannot be in the future."}),
    ({"worked_on": (OPENED - timedelta(days=1)).isoformat()}, {"worked_on": "time cannot be logged before that"}),
    ({"worked_on": "10/05/2026"}, {"worked_on": "Enter the date the work was done."}),
    ({"worked_on": ""}, {"worked_on": "Enter the date the work was done."}),
    ({"description": "x" * 121}, {"description": "Keep the description to 120 characters."}),
    ({"hours": "0", "worked_on": ""}, {"hours": "more than 0", "worked_on": "Enter the date"}),  # every problem at once
    ({"hours": ["1"]}, {"hours": "Not a valid string."}),
    ({"hours": True}, {"hours": "Not a valid string."}),
    ({"description": {"a": 1}}, {"description": "Not a valid string."}),
    ({"description": "Fixed\x07"}, {"description": "Remove the invisible control character from this text."}),
    ({"technician": "not-an-id"}, {"technician": "Choose a technician from this facility."}),
    ({"minutes": "30"}, {"minutes": "Unknown field."}),
])
def test_a_refused_line_is_a_400_keyed_by_field(client, signed_in, wo, change, errors):
    signed_in("technician")
    r = post(client, url(wo, "labor"), {**LABOR, **change})
    assert r.status_code == 400 and set(r.json()) == set(errors), r.json()
    for field, text in errors.items():
        assert text in r.json()[field][0], r.json()
    assert not LaborLine.objects.exists()


def test_a_body_that_is_not_an_object_is_refused(client, signed_in, wo):
    signed_in("technician")
    for body in (["hours", "1"], "1.5", 3):
        assert post(client, url(wo, "labor"), body).status_code == 400
    assert not LaborLine.objects.exists()


def test_one_technician_logs_at_most_24_hours_a_day(client, signed_in, wo, vent, techs):
    other = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm", opened_on=OPENED, assigned_to=techs["dana"])
    signed_in("technician")
    assert post(client, url(other, "labor"), {**LABOR, "hours": "20"}).status_code == 201
    r = post(client, url(wo, "labor"), {**LABOR, "hours": "4.5"})
    assert r.status_code == 400 and r.json()["hours"] == [f"Dana Whitfield already has 20 h logged on {TODAY:%b %-d, %Y}; one day holds at most 24 h."]
    assert post(client, url(wo, "labor"), {**LABOR, "hours": "4"}).status_code == 201


def test_the_technician_is_one_of_this_facilitys(client, signed_in, wo, techs, theirs):
    signed_in("technician")
    r = post(client, url(wo, "labor"), {**LABOR, "technician": str(theirs["tech"].pk)})
    assert r.status_code == 400 and r.json() == {"technician": ["Choose a technician from this facility."]}
    Technician.objects.filter(pk=techs["tom"].pk).update(is_active=False)
    r = post(client, url(wo, "labor"), {**LABOR, "technician": str(techs["tom"].pk)})
    assert r.status_code == 400 and r.json() == {"technician": ["Tom Okafor is no longer active. Choose another technician."]}
    assert not LaborLine.objects.exists()


def test_removing_a_labor_line(client, signed_in, wo, vendor_wo):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    elsewhere = costs.add_labor(vendor_wo, hours="1", worked_on=TODAY, by=None)
    user = signed_in("technician")  # Edit removes a line, as on the drawer; deleting is not Full here
    assert client.delete(url(wo, "labor", elsewhere)).status_code == 404  # a line under another work order
    assert client.delete(f"{url(wo, 'labor')}not-an-id/").status_code == 404
    r = client.delete(url(wo, "labor", line))
    assert r.status_code == 204 and not LaborLine.objects.filter(pk=line.pk).exists() and LaborLine.objects.filter(pk=elsewhere.pk).exists()
    h = LaborLine.history.filter(id=line.pk, history_type="-").get()
    assert (h.history_change_reason, h.history_user) == (costs.CHANGE_REMOVED, user)
    assert client.delete(url(wo, "labor", line)).status_code == 404  # already removed
    assert timeline(wo)[-1]["text"].startswith("Labor removed: 1 h logged by Dana Whitfield")


def test_a_closed_work_order_is_the_record_and_a_cancelled_one_takes_nothing(client, signed_in, wo, vendor_wo):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    p = costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    for to in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED, WoStatus.CLOSED):
        change_status(wo, to)
    change_status(vendor_wo, WoStatus.CANCELLED)
    signed_in("director")
    for what, body, existing in (("labor", LABOR, line), ("parts", PART, p)):
        r = post(client, url(wo, what), body)
        assert r.status_code == 400 and "is closed: its labor and parts are the record" in r.json()["detail"]
        r = client.delete(url(wo, what, existing))
        assert r.status_code == 400 and "is closed" in r.json()["detail"]
        r = post(client, url(vendor_wo, what), body)
        assert r.status_code == 400 and "was cancelled: it takes no labor or parts" in r.json()["detail"]
        listed = client.get(url(wo, what)).json()
        assert listed["can_record"] is False and "is closed" in listed["locked"]
    assert LaborLine.objects.count() == 1 and PartLine.objects.count() == 1


# --- parts -----------------------------------------------------------------------------------------------------------------------

def test_add_part_goes_through_the_service(client, signed_in, wo):
    user = signed_in("technician")
    r = post(client, url(wo, "parts"), PART)
    assert r.status_code == 201, r.content
    line = PartLine.objects.get()
    assert r.json() == {"id": str(line.pk), "description": "Pump door latch", "part_number": "10013-B", "quantity": "2.00", "unit_cost": "42.00",
                        "po_number": "PO-5566", "amount": "84.00", "created_at": r.json()["created_at"]}
    assert PartLine.history.get(id=line.pk).history_user == user
    assert timeline(wo)[-1]["text"] == "Part: Pump door latch × 2 ($84.00)"
    r = post(client, url(wo, "parts"), {"description": "Battery", "quantity": 1, "unit_cost": "$1,250.00"})  # read as the drawer reads it
    assert r.status_code == 201 and r.json()["unit_cost"] == "1250.00"
    data = client.get(url(wo, "parts")).json()
    assert (data["count"], data["total"], data["can_record"], data["locked"]) == (2, "1334.00", True, "")
    assert [p["description"] for p in data["results"]] == ["Pump door latch", "Battery"]


def test_the_api_and_the_drawer_add_the_same_part(client, signed_in, wo):
    signed_in("technician")
    body = {**PART, "quantity": "3", "unit_cost": "19.99"}
    assert client.post(f"/work-orders/{wo.number}/parts/", body, **HX).status_code == 200
    assert post(client, url(wo, "parts"), body).status_code == 201
    web, api = PartLine.objects.order_by("created_at")
    fields = ("description", "part_number", "quantity", "unit_cost", "po_number")
    assert [getattr(web, f) for f in fields] == [getattr(api, f) for f in fields] and costs.part_amount(api) == Decimal("59.97")


@pytest.mark.parametrize("change, field, text", [
    ({"description": "  "}, "description", "Describe the part."),
    ({"quantity": "0"}, "quantity", "Enter a quantity above 0"),
    ({"quantity": None}, "quantity", "Enter the quantity."),
    ({"unit_cost": "-1"}, "unit_cost", "The unit cost is between $0"),
    ({"unit_cost": "4.255"}, "unit_cost", "Use at most 2 decimal places."),
    ({"po_number": "P" * 41}, "po_number", "Keep the PO number to 40 characters."),
    ({"vendor": "Acme"}, "vendor", "Unknown field."),
    ({"quantity": [2]}, "quantity", "Not a valid string."),
])
def test_a_refused_part_is_a_400_keyed_by_field(client, signed_in, wo, change, field, text):
    signed_in("technician")
    r = post(client, url(wo, "parts"), {**PART, **change})
    assert r.status_code == 400 and list(r.json()) == [field] and text in r.json()[field][0], r.json()
    assert not PartLine.objects.exists()


def test_removing_a_part_line(client, signed_in, wo, vendor_wo):
    p = costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    elsewhere = costs.add_part(vendor_wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    signed_in("technician")
    assert client.delete(url(wo, "parts", elsewhere)).status_code == 404
    assert client.delete(url(wo, "parts", p)).status_code == 204
    assert list(PartLine.objects.all()) == [elsewhere] and timeline(wo)[-1]["text"] == "Part removed: Fuse ($2.00)"


# --- notes and the timeline ------------------------------------------------------------------------------------------------------

def test_the_timeline_is_the_drawers(client, signed_in, wo, techs):
    user = signed_in("technician")
    change_status(wo, WoStatus.IN_PROGRESS, by=user)
    add_note(wo, "Ordered a latch", by=user)
    costs.add_labor(wo, hours="1", worked_on=TODAY, by=user)
    data = client.get(url(wo, "notes")).json()
    assert [e["text"] for e in data["results"]] == [e["text"] for e in timeline(wo)] == [
        "Opened: Door latch broken", "Status changed to in progress", "Ordered a latch", "1 h logged by Dana Whitfield"]
    assert [e["who"] for e in data["results"]] == [e["who"] for e in timeline(wo)] == ["System", "Technician User", "Technician User", "Technician User"]
    assert data["count"] == 4 and data["can_note"] is True and data["work_order"] == wo.number


def test_add_a_note(client, signed_in, wo):
    user = signed_in("technician")
    r = post(client, url(wo, "notes"), {"text": "  Called the unit back  "})
    assert r.status_code == 201, r.content
    note = WorkOrderNote.objects.get()
    assert r.json() == {"id": str(note.pk), "at": r.json()["at"], "who": "Technician User", "text": "Called the unit back"}
    assert (note.author, note.author_name, note.work_order) == (user, "Technician User", wo)
    assert client.get(url(wo, "notes")).json()["results"][-1] == {k: v for k, v in r.json().items() if k != "id"}
    # The drawer's form adds the same note the same way.
    assert client.post(f"/work-orders/{wo.number}/notes/", {"text": "Called the unit back"}, **HX).status_code == 200
    web, api = WorkOrderNote.objects.order_by("-created_at")[:2]
    assert (web.text, web.author_name) == (api.text, api.author_name)


@pytest.mark.parametrize("body, errors", [
    ({"text": "   "}, {"text": ["A note needs some text."]}),
    ({}, {"text": ["A note needs some text."]}),
    ({"text": "x" * 1001}, {"text": ["Notes are limited to 1000 characters."]}),
    ({"text": ["a"]}, {"text": ["Not a valid string."]}),
    ({"text": "ok", "author": "Someone else"}, {"author": ["Unknown field."]}),
])
def test_a_refused_note_is_a_400(client, signed_in, wo, body, errors):
    signed_in("technician")
    r = post(client, url(wo, "notes"), body)
    assert r.status_code == 400 and r.json() == errors
    assert not WorkOrderNote.objects.exists()


# --- who may do what -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role, may_record, _may_rate", ROLES)
def test_every_endpoint_checks_the_level_server_side(client, signed_in, wo, role, may_record, _may_rate):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    p = costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    share(signed_in(role), wo)
    for what in ("labor", "parts", "notes"):
        assert client.get(url(wo, what)).status_code == 200, (role, what)  # every role here holds work orders View
    ok = (201, 204) if may_record else (403, 403)
    assert post(client, url(wo, "labor"), LABOR).status_code == ok[0]
    assert post(client, url(wo, "parts"), PART).status_code == ok[0]
    assert post(client, url(wo, "notes"), {"text": "Checked"}).status_code == ok[0]
    assert client.delete(url(wo, "labor", line)).status_code == ok[1]
    assert client.delete(url(wo, "parts", p)).status_code == ok[1]
    assert LaborLine.objects.filter(pk=line.pk).exists() != may_record and PartLine.objects.filter(pk=p.pk).exists() != may_record
    assert LaborLine.objects.count() == PartLine.objects.count() == 1 and WorkOrderNote.objects.count() == (1 if may_record else 0)
    if not may_record:
        assert client.get(url(wo, "labor")).json()["can_record"] is False


def test_without_work_orders_access_everything_is_refused(client, ctx, make_user, wo):
    role = Role.objects.create(name="Contracts clerk", slug="clerk")
    role.set_levels({Module.CONTRACTS: 3})
    user = make_user("analyst")
    user.role = role
    user.save()
    client.force_login(user)
    for what in ("labor", "parts", "notes"):
        assert client.get(url(wo, what)).status_code == 403
        assert post(client, url(wo, what), {}).status_code == 403


def test_signed_out_is_refused(client, wo):
    for what in ("labor", "parts", "notes"):
        assert client.get(url(wo, what)).status_code == 403
        assert post(client, url(wo, what), LABOR).status_code == 403
    assert not LaborLine.objects.exists()


def test_another_facilitys_work_order_and_lines_are_404(client, signed_in, wo, theirs):
    signed_in("director")
    for what, body, line in (("labor", LABOR, theirs["labor"]), ("parts", PART, theirs["part"]), ("notes", {"text": "x"}, None)):
        assert client.get(url(theirs["wo"], what)).status_code == 404
        assert post(client, url(theirs["wo"], what), body).status_code == 404
        if line is not None:
            assert client.delete(url(theirs["wo"], what, line)).status_code == 404
            assert client.delete(url(wo, what, line)).status_code == 404  # their line under our work order
    assert client.get(f"{API}not-an-id/labor/").status_code == 404
    with tenant_context(theirs["wo"].tenant):
        assert LaborLine.objects.count() == PartLine.objects.count() == 1 and not WorkOrderNote.objects.exists()


def test_other_methods_are_not_offered(client, signed_in, wo):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    signed_in("director")
    assert client.patch(url(wo, "labor", line), {"hours": "2"}, content_type="application/json").status_code == 405
    assert client.get(url(wo, "labor", line)).status_code == 405
    assert client.delete(url(wo, "notes")).status_code == 405


def test_the_browsable_api_renders_them(client, signed_in, wo, vendor_wo):
    costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    signed_in("director")
    for what in ("labor", "parts", "notes"):
        r = client.get(url(wo, what), HTTP_ACCEPT="text/html")
        assert r.status_code == 200 and wo.number in r.content.decode(), what
    signed_in("vendor", company="Hamilton Medical")
    assert client.get(url(vendor_wo, "labor"), HTTP_ACCEPT="text/html").status_code == 200
    assert client.get(url(wo, "labor"), HTTP_ACCEPT="text/html").status_code == 404


def test_a_superuser_without_a_facility_is_told_to_pick_one(client, db, wo):
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    client.force_login(root)
    r = client.get(url(wo, "labor"))
    assert r.status_code == 403 and "Pick a tenant first" in r.json()["detail"]


# --- scoped users ----------------------------------------------------------------------------------------------------------------

def test_the_vendor_records_on_their_companys_work_orders_only(client, signed_in, wo, vendor_wo, theirs):
    signed_in("vendor", company="Hamilton Medical")  # work orders Edit; sees vendor_wo, not the in-house wo
    r = post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "1.25"})
    assert r.status_code == 201 and (r.json()["technician"], r.json()["rate"], r.json()["amount"]) == (None, "215.00", "268.75")
    assert post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "1", "rate": "300"}).status_code == 403
    p = post(client, url(vendor_wo, "parts"), PART)
    assert p.status_code == 201
    assert post(client, url(vendor_wo, "notes"), {"text": "Sensor on order"}).status_code == 201
    assert client.get(url(vendor_wo, "labor")).json()["count"] == 1 and client.get(url(vendor_wo, "parts")).json()["count"] == 1
    line = LaborLine.objects.get(work_order=vendor_wo)
    assert client.delete(url(vendor_wo, "labor", line)).status_code == 204
    assert client.delete(f"{url(vendor_wo, 'parts')}{p.json()['id']}/").status_code == 204
    # The facility's in-house work order, and another facility's, are not there for them.
    mine = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    for target in (wo, theirs["wo"]):
        for what, body in (("labor", LABOR), ("parts", PART), ("notes", {"text": "x"})):
            assert client.get(url(target, what)).status_code == 404, what
            assert post(client, url(target, what), body).status_code == 404, what
    assert client.delete(url(wo, "labor", mine)).status_code == 404
    assert LaborLine.objects.filter(work_order=wo).count() == 1 and not PartLine.objects.filter(work_order=wo).exists()
    assert not WorkOrderNote.objects.filter(work_order=wo).exists()


def test_the_requester_reads_their_units_work_and_records_nothing(client, signed_in, wo, ctx):
    cardio = Department.objects.create(name="Cardiology")
    card_pump = Asset.objects.create(tag="CE-20002", device_model=wo.asset.device_model, department=cardio)
    elsewhere = create_work_order(asset=card_pump, type="repair", priority="normal", problem="Keypad", opened_on=OPENED)
    costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    signed_in("requester", department="icu")  # work orders Request
    assert client.get(url(wo, "labor")).json()["count"] == 1 and client.get(url(wo, "notes")).status_code == 200
    for what in ("labor", "parts", "notes"):
        assert client.get(url(elsewhere, what)).status_code == 404
        assert post(client, url(wo, what), {}).status_code == 403  # their level refuses before any record is looked up


def test_a_unit_scoped_role_with_edit_records_in_its_unit_only(client, ctx, make_user, wo, techs):
    cardio = Department.objects.create(name="Cardiology")
    card_pump = Asset.objects.create(tag="CE-20002", device_model=wo.asset.device_model, department=cardio)
    elsewhere = create_work_order(asset=card_pump, type="repair", priority="normal", problem="Keypad", opened_on=OPENED, assigned_to=techs["tom"])
    role = Role.objects.create(name="Unit tech", slug="unit-tech", scope=DataScope.DEPARTMENT)
    role.set_levels({Module.WORKORDERS: 3})
    user = make_user("technician")
    user.role, user.department = role, "ICU"
    user.save()
    client.force_login(user)
    assert post(client, url(wo, "labor"), LABOR).status_code == 201
    assert post(client, url(elsewhere, "labor"), LABOR).status_code == 404
    assert not LaborLine.objects.filter(work_order=elsewhere).exists()


def test_a_scoped_user_reads_the_timeline_as_the_drawer_shows_it(client, signed_in, wo, vendor_wo):
    """A note on the vendor's work order names the facility's in-house one: the vendor reads "another work order" there, as in the
    drawer; the facility reads the number."""
    add_note(vendor_wo, f"Same fault as {wo.number}; see {vendor_wo.number}")
    signed_in("vendor", company="Hamilton Medical")
    text = client.get(url(vendor_wo, "notes")).json()["results"][-1]["text"]
    assert text == f"Same fault as another work order; see {vendor_wo.number}"
    r = post(client, url(vendor_wo, "notes"), {"text": f"Not {wo.number}"})
    assert r.status_code == 201 and r.json()["text"] == "Not another work order"
    body = client.get(f"/work-orders/{vendor_wo.number}/", **HX).content.decode()
    assert "Same fault as another work order" in body and wo.number not in body  # the drawer, for comparison
    signed_in("director")
    assert client.get(url(vendor_wo, "notes")).json()["results"][-2]["text"] == f"Same fault as {wo.number}; see {vendor_wo.number}"


# --- tokens ----------------------------------------------------------------------------------------------------------------------

def test_every_endpoint_by_token(client, make_user, tenant, wo, techs):
    """Token requests carry no session: the views set the token's facility (TenantAPIMixin) and the lines land in it."""
    auth = token(make_user, "technician")
    r = post(client, url(wo, "labor"), LABOR, **auth)
    assert r.status_code == 201, r.content
    assert client.get(url(wo, "labor"), **auth).json()["count"] == 1
    p = post(client, url(wo, "parts"), PART, **auth)
    assert p.status_code == 201 and client.get(url(wo, "parts"), **auth).json()["total"] == "84.00"
    assert post(client, url(wo, "notes"), {"text": "By token"}, **auth).status_code == 201
    assert client.get(url(wo, "notes"), **auth).json()["results"][-1]["text"] == "By token"
    assert client.delete(f"{url(wo, 'labor')}{r.json()['id']}/", **auth).status_code == 204
    assert client.delete(f"{url(wo, 'parts')}{p.json()['id']}/", **auth).status_code == 204
    with tenant_context(tenant):
        assert not LaborLine.objects.exists() and not PartLine.objects.exists() and WorkOrderNote.objects.get().tenant_id == tenant.id


def test_tokens_are_refused_what_sessions_are(client, make_user, wo, vendor_wo):
    analyst = token(make_user, "analyst")
    assert client.get(url(wo, "labor"), **analyst).status_code == 200
    assert post(client, url(wo, "labor"), LABOR, **analyst).status_code == 403
    vendor = token(make_user, "vendor", company="Hamilton Medical")
    assert post(client, url(wo, "labor"), LABOR, **vendor).status_code == 404
    assert post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "1"}, **vendor).status_code == 201
    assert post(client, url(wo, "notes"), {"text": "x"}, **vendor).status_code == 404


@needs_postgres
def test_the_endpoints_by_token_under_the_policies(client, make_user, tenant, wo, vendor_wo, theirs):
    """As the runtime role, by token: logging time (which locks the work order and the technician), adding a part, a note, the
    timeline, and removing lines all work inside the token's facility, and another facility's work order stays a 404."""
    auth = token(make_user, "technician")
    vendor = token(make_user, "vendor", company="Hamilton Medical")
    as_app_role()
    r = post(client, url(wo, "labor"), LABOR, **auth)
    assert r.status_code == 201, r.content
    p = post(client, url(wo, "parts"), PART, **auth)
    assert p.status_code == 201, p.content
    assert post(client, url(wo, "notes"), {"text": "Under the policies"}, **auth).status_code == 201
    assert [e["text"] for e in client.get(url(wo, "notes"), **auth).json()["results"]][-3:] == [
        "1.5 h logged by Dana Whitfield", "Part: Pump door latch × 2 ($84.00)", "Under the policies"]
    assert client.get(url(wo, "labor"), **auth).json()["total"] == "123.00"
    assert post(client, url(vendor_wo, "labor"), {"worked_on": TODAY.isoformat(), "hours": "1"}, **vendor).status_code == 201
    assert client.get(url(wo, "labor"), **vendor).status_code == 404
    assert client.get(url(theirs["wo"], "labor"), **auth).status_code == 404
    assert client.delete(url(theirs["wo"], "labor", theirs["labor"]), **auth).status_code == 404
    assert client.delete(f"{url(wo, 'labor')}{r.json()['id']}/", **auth).status_code == 204
    assert client.delete(f"{url(wo, 'parts')}{p.json()['id']}/", **auth).status_code == 204
    with tenant_context(tenant):
        assert not LaborLine.objects.filter(work_order=wo).exists() and LaborLine.objects.filter(work_order=vendor_wo).count() == 1
        assert WorkOrderNote.objects.get().text == "Under the policies"
