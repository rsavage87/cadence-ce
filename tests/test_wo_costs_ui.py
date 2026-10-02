"""Labor and parts in the work order drawer, and the labor rates on the Settings screen (slice 15, part A). Permissions are checked
server-side on every view, GET and POST (a hidden button is not access control); another facility's work order or line is a 404;
every change answers with the drawer, toasts, and fires wo-changed; the modals follow the shell's HTMX conventions."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounts.models import create_default_roles
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.workorders import costs
from apps.workorders.models import LaborLine, PartLine, WoStatus
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
OPENED = TODAY - timedelta(days=5)
# role, may record work (work orders Edit), may set a rate (work orders Approve)
ROLES = [("director", True, True), ("manager", True, True), ("technician", True, False), ("vendor", True, False), ("analyst", False, False),
         ("requester", False, False)]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
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


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def share(user, wo):
    """Slice 16: a clinical requester sees their unit's work orders and a vendor technician their company's; give them this one."""
    user.department, user.company = "ICU", "BD"
    user.save()
    if user.role.slug == "vendor":
        assign(wo, vendor_name="BD field service")


def urls(wo, line=None, p=None) -> dict:
    base = f"/work-orders/{wo.number}"
    return {"labor": f"{base}/labor/", "part": f"{base}/parts/", "labor_delete": f"{base}/labor/{line.pk}/delete/" if line else None,
            "part_delete": f"{base}/parts/{p.pk}/delete/" if p else None}


LABOR_POST = {"worked_on": TODAY.isoformat(), "hours": "1.5", "description": "Replaced latch"}
PART_POST = {"description": "Pump door latch", "part_number": "10013-B", "quantity": "2", "unit_cost": "42", "po_number": "PO-5566"}


# --- who may do what -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role, may_record, _may_rate", ROLES)
def test_every_view_checks_the_level_server_side(client, signed_in, wo, techs, role, may_record, _may_rate):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    p = costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    u = urls(wo, line, p)
    share(signed_in(role), wo)
    ok = 200 if may_record else 403
    assert client.get(u["labor"], **HX).status_code == ok
    assert client.get(u["part"], **HX).status_code == ok
    assert client.post(u["labor"], {**LABOR_POST, "technician": str(techs["dana"].pk)}, **HX).status_code == ok
    assert client.post(u["part"], PART_POST, **HX).status_code == ok
    assert client.post(u["labor_delete"], **HX).status_code == ok
    assert client.post(u["part_delete"], **HX).status_code == ok
    # lines added and removed only when allowed
    assert LaborLine.objects.filter(work_order=wo).count() == 1 and PartLine.objects.filter(work_order=wo).count() == 1
    assert LaborLine.objects.filter(pk=line.pk).exists() != may_record and PartLine.objects.filter(pk=p.pk).exists() != may_record


@pytest.mark.parametrize("role, may_record, may_rate", ROLES)
def test_the_drawer_offers_what_the_role_may_do(client, signed_in, wo, role, may_record, may_rate):
    costs.add_labor(wo, hours="1.5", worked_on=TODAY, by=None)
    share(signed_in(role), wo)
    r = client.get(f"/work-orders/{wo.number}/", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "1.5 h · Dana Whitfield" in body and "$123.00" in body
    assert ("Log time</button>" in body) == may_record and ("Add part</button>" in body) == may_record
    assert ("/delete/" in body) == may_record  # Remove
    if may_record:
        modal = client.get(f"/work-orders/{wo.number}/labor/", **HX).content.decode()
        assert ('name="rate"' in modal) == may_rate
        charged = "for vendor service, $215.00" if role == "vendor" else "for in-house labor, $82.00"  # share() made it the vendor's
        assert (f"Charged at the Settings rate {charged} an hour." in modal) != may_rate


def test_a_different_rate_needs_approve(client, signed_in, wo, techs):
    url = f"/work-orders/{wo.number}/labor/"
    signed_in("technician")
    assert client.post(url, {**LABOR_POST, "rate": "95"}, **HX).status_code == 403
    assert client.post(url, {**LABOR_POST, "rate": "abc"}, **HX).status_code == 403
    assert not LaborLine.objects.exists()
    assert client.post(url, {**LABOR_POST, "hours": "1", "rate": "$82.00"}, **HX).status_code == 200  # the Settings rate, typed out
    assert client.post(url, {**LABOR_POST, "hours": "1", "rate": ""}, **HX).status_code == 200
    assert set(LaborLine.objects.values_list("rate", flat=True)) == {Decimal("82.00")}
    signed_in("manager")
    r = client.post(url, {**LABOR_POST, "rate": "$1,250.00"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    assert LaborLine.objects.filter(rate=Decimal("1250")).count() == 1
    modal = client.get(url, **HX).content.decode()
    assert 'name="rate"' in modal and 'placeholder="82"' in modal and "Blank charges the Settings rate, $82.00 an hour." in modal


def test_the_modals_are_only_modals_and_removing_needs_post(client, signed_in, wo):
    line = costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    p = costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    signed_in("technician")
    for url in (f"/work-orders/{wo.number}/labor/", f"/work-orders/{wo.number}/parts/"):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"] == f"/work-orders/{wo.number}/"
    assert client.get(f"/work-orders/{wo.number}/labor/{line.pk}/delete/").status_code == 405
    assert client.get(f"/work-orders/{wo.number}/parts/{p.pk}/delete/").status_code == 405


def test_signed_out_is_sent_to_sign_in(client, wo):
    r = client.get(f"/work-orders/{wo.number}/labor/", **HX)
    assert r.status_code == 302 and r["Location"].startswith("/login/")


# --- another facility --------------------------------------------------------------------------------------------------------------

def test_another_facilitys_work_order_and_lines_are_404(client, signed_in, wo, theirs):
    signed_in("director")
    t = urls(theirs["wo"], theirs["labor"], theirs["part"])
    assert client.get(t["labor"], **HX).status_code == 404
    assert client.get(t["part"], **HX).status_code == 404
    assert client.post(t["labor"], LABOR_POST, **HX).status_code == 404
    assert client.post(t["part"], PART_POST, **HX).status_code == 404
    assert client.post(t["labor_delete"], **HX).status_code == 404
    assert client.post(t["part_delete"], **HX).status_code == 404
    # their line under our work order's address
    ours = urls(wo, theirs["labor"], theirs["part"])
    assert client.post(ours["labor_delete"], **HX).status_code == 404
    assert client.post(ours["part_delete"], **HX).status_code == 404
    # unscoped: their rows, counted across tenants, are untouched
    assert LaborLine.unscoped.filter(pk=theirs["labor"].pk).exists() and PartLine.unscoped.filter(pk=theirs["part"].pk).exists()
    assert not LaborLine.objects.exists() and not PartLine.objects.exists()


def test_a_line_from_another_work_order_is_404_under_this_one(client, signed_in, wo, vendor_wo):
    line = costs.add_labor(vendor_wo, hours="1", worked_on=TODAY, by=None)
    signed_in("technician")
    assert client.post(f"/work-orders/{wo.number}/labor/{line.pk}/delete/", **HX).status_code == 404
    assert LaborLine.objects.filter(pk=line.pk).exists()


def test_another_facilitys_technician_cannot_be_chosen(client, signed_in, wo, theirs):
    signed_in("technician")
    r = client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "technician": str(theirs["tech"].pk)}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "Choose a technician from the list." in r.content.decode()
    assert not LaborLine.objects.exists()


# --- Log time ----------------------------------------------------------------------------------------------------------------------

def test_log_time_modal(client, signed_in, wo, techs):
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/labor/", **HX).content.decode()
    assert f"<h2>Log time on {wo.number}</h2>" in body and f'hx-post="/work-orders/{wo.number}/labor/" hx-target="#modal-card"' in body
    assert re.search(rf'<option value="{techs["dana"].pk}" selected>Dana Whitfield</option>', body)  # the assigned technician
    assert f'value="{TODAY.isoformat()}"' in body and f'min="{OPENED.isoformat()}"' in body and f'max="{TODAY.isoformat()}"' in body
    assert "What was done (no patient information)" in body and 'maxlength="120"' in body and 'inputmode="decimal"' in body
    assert re.search(r'<input[^>]*name="hours"[^>]*autofocus', body)


def test_logging_time_answers_with_the_drawer(client, signed_in, wo, techs):
    user = signed_in("technician")
    r = client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "technician": str(techs["tom"].pk)}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    assert "wo-changed" in triggers(r) and toast_of(r) == f"1.5 h logged on {wo.number}"
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    line = LaborLine.objects.get()
    assert line.technician == techs["tom"] and line.rate == Decimal("82.00") and line.description == "Replaced latch"
    assert line.history.get().history_user == user
    body = r.content.decode()
    assert f"<h2>{wo.number} · Corrective repair</h2>" in body  # the drawer
    assert "1.5 h · Tom Okafor" in body and f"{TODAY:%b} {TODAY.day}, {TODAY.year} · $82.00/h · Replaced latch" in body and "$123.00" in body
    assert "1.5 h logged by Tom Okafor" in body  # the timeline
    assert "labor at $82.00/h · $123.00" in body


def test_a_refused_line_keeps_the_modal_and_what_was_typed(client, signed_in, wo):
    signed_in("technician")
    r = client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "hours": "1.555", "description": "Tested"}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "toast" not in triggers(r)
    assert "Use at most 2 decimal places." in body and 'value="1.555"' in body and 'value="Tested"' in body
    assert re.search(r'<input[^>]*name="hours"[^>]*autofocus|<input[^>]*autofocus[^>]*name="hours"', body)
    assert not LaborLine.objects.exists()
    r = client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "worked_on": "not a date"}, **HX)
    assert "Enter the date the work was done." in r.content.decode() and not LaborLine.objects.exists()


def test_vendor_service_time_has_no_technician_and_the_vendor_rate(client, signed_in, vendor_wo):
    signed_in("technician")
    body = client.get(f"/work-orders/{vendor_wo.number}/labor/", **HX).content.decode()
    assert 'name="technician"' not in body and "Vendor service, Hamilton Medical field service" in body
    assert "Charged at the Settings rate for vendor service, $215.00 an hour." in body
    r = client.post(f"/work-orders/{vendor_wo.number}/labor/", {"worked_on": TODAY.isoformat(), "hours": "2"}, **HX)
    assert toast_of(r) == f"2 h logged on {vendor_wo.number}"
    line = LaborLine.objects.get()
    assert line.technician is None and line.rate == Decimal("215.00")
    assert "2 h · Hamilton Medical field service" in r.content.decode()


def test_a_new_settings_rate_prices_time_logged_afterwards(client, signed_in, wo):
    signed_in("director")
    client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "hours": "1"}, **HX)
    fs.update_settings(labor_rate="90")
    client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "hours": "1"}, **HX)
    assert sorted(LaborLine.objects.values_list("rate", flat=True)) == [Decimal("82.00"), Decimal("90.00")]
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "labor · $172.00" in body and "labor at" not in body  # two rates: no single rate to name


# --- Add part --------------------------------------------------------------------------------------------------------------------

def test_add_part_modal_and_save(client, signed_in, wo):
    user = signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/parts/", **HX).content.decode()
    assert f"<h2>Add a part to {wo.number}</h2>" in body and 'name="quantity" value="1"' in body and "(no patient information)" in body
    r = client.post(f"/work-orders/{wo.number}/parts/", {**PART_POST, "unit_cost": "$42.00"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and toast_of(r) == "Part added" and "wo-changed" in triggers(r)
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    p = PartLine.objects.get()
    assert p.unit_cost == Decimal("42") and p.quantity == Decimal("2") and p.history.get().history_user == user
    out = r.content.decode()
    assert "Pump door latch × 2" in out and "PN 10013-B · 2 at $42.00 · PO PO-5566" in out and "$84.00" in out
    assert "Part: Pump door latch × 2 ($84.00)" in out  # the timeline
    assert "parts · 1 line" in out


def test_a_refused_part_keeps_the_modal(client, signed_in, wo):
    signed_in("technician")
    r = client.post(f"/work-orders/{wo.number}/parts/", {**PART_POST, "description": " ", "quantity": "0"}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert "Describe the part." in body and "Enter a quantity above 0 and at most 9,999." in body and not PartLine.objects.exists()


# --- removing --------------------------------------------------------------------------------------------------------------------

def test_removing_answers_with_the_drawer(client, signed_in, wo):
    line = costs.add_labor(wo, hours="1.5", worked_on=TODAY, by=None)
    p = costs.add_part(wo, description="Pump door latch", quantity="2", unit_cost="42", by=None)
    user = signed_in("technician")
    drawer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert f'hx-post="/work-orders/{wo.number}/labor/{line.pk}/delete/" hx-target="#drawer"' in drawer
    assert f'hx-confirm="Remove 1.5 h by Dana Whitfield on {TODAY:%b} {TODAY.day}, {TODAY.year}?"' in drawer
    assert 'hx-confirm="Remove Pump door latch × 2 ($84.00)?"' in drawer
    r = client.post(f"/work-orders/{wo.number}/labor/{line.pk}/delete/", **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and toast_of(r) == "Labor line removed" and "wo-changed" in triggers(r)
    assert not LaborLine.objects.exists() and LaborLine.history.get(id=line.pk, history_type="-").history_user == user
    assert "Labor removed: 1.5 h logged by Dana Whitfield" in r.content.decode()
    r = client.post(f"/work-orders/{wo.number}/parts/{p.pk}/delete/", **HX)
    assert toast_of(r) == "Part removed" and not PartLine.objects.exists() and "Part removed: Pump door latch × 2 ($84.00)" in r.content.decode()


# --- closed and cancelled ----------------------------------------------------------------------------------------------------------

def test_a_closed_work_order_offers_no_changes_and_refuses_them(client, signed_in, wo):
    line = costs.add_labor(wo, hours="1.5", worked_on=OPENED, by=None)
    for to in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED, WoStatus.CLOSED):
        change_status(wo, to, as_of=OPENED)
    reason = f"{wo.number} is closed: its labor and parts are the record. Reopen it to change them."
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "Log time</button>" not in body and "/delete/" not in body and reason in body and "Cost <span>actual</span>" in body
    modal = client.get(f"/work-orders/{wo.number}/labor/", **HX).content.decode()
    assert reason in modal and "<form" not in modal
    assert reason in client.get(f"/work-orders/{wo.number}/parts/", **HX).content.decode()
    r = client.post(f"/work-orders/{wo.number}/labor/", {**LABOR_POST, "worked_on": OPENED.isoformat()}, **HX)
    assert r.status_code == 200 and reason in r.content.decode() and "HX-Retarget" not in r
    r = client.post(f"/work-orders/{wo.number}/labor/{line.pk}/delete/", **HX)
    assert r.status_code == 200 and toast_of(r) == reason and "wo-changed" not in triggers(r)
    assert LaborLine.objects.filter(pk=line.pk).exists()


def test_a_cancelled_work_order_says_why_to_those_who_could_record(client, signed_in, wo):
    change_status(wo, WoStatus.CANCELLED)
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "Log time</button>" not in body and "was cancelled: it takes no labor or parts." in body and "No time logged." in body


def test_viewers_see_the_lines_without_the_reason(client, signed_in, wo):
    costs.add_part(wo, description="Pump door latch", quantity="2", unit_cost="42", by=None)
    change_status(wo, WoStatus.CANCELLED)
    signed_in("analyst")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "Pump door latch × 2" in body and "was cancelled" not in body and "/delete/" not in body


def test_an_open_drawer_without_lines(client, signed_in, wo):
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "Cost <span>so far</span>" in body and "No time logged yet." in body and "No parts yet." in body and "0 h" in body
    assert f'hx-get="/work-orders/{wo.number}/labor/" hx-target="#modal-card"' in body
    assert f'hx-get="/work-orders/{wo.number}/parts/" hx-target="#modal-card"' in body


def test_the_drawer_runs_a_fixed_number_of_queries_whatever_the_lines(client, signed_in, wo, techs):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    costs.add_labor(wo, hours="1", worked_on=TODAY, by=None)
    costs.add_part(wo, description="Fuse", quantity="1", unit_cost="2", by=None)
    signed_in("technician")
    url = f"/work-orders/{wo.number}/"
    client.get(url, **HX)
    with CaptureQueriesContext(connection) as before:
        client.get(url, **HX)
    for i in range(5):
        line = costs.add_labor(wo, hours="1", worked_on=TODAY, technician=techs["tom" if i % 2 else "dana"], by=None)
        costs.add_part(wo, description=f"Part {i}", quantity="1", unit_cost="2", by=None)
        if i == 4:
            costs.remove_labor(line, by=None)  # removals are told from the history too
    with CaptureQueriesContext(connection) as after:
        client.get(url, **HX)
    assert len(after) == len(before)


def test_the_full_page_drawer_renders_the_cost_section(client, signed_in, wo):
    costs.add_labor(wo, hours="1.5", worked_on=TODAY, by=None)
    signed_in("technician")
    r = client.get(f"/work-orders/{wo.number}/")
    assert r.status_code == 200 and "1.5 h · Dana Whitfield" in r.content.decode()


# --- the labor rates on the Settings screen ---------------------------------------------------------------------------------------

TARGETS = {"target_pm_pct": "95", "target_uptime_pct": "99.5", "target_mttr_days": "3", "repair_budget_monthly": ""}


def test_the_settings_panel_shows_the_rates(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/").content.decode()
    assert "KPI targets and labor rates" in body and "Labor rates" in body and "Save targets and rates" in body
    assert 'name="labor_rate" value="82"' in body and 'name="vendor_labor_rate" value="215"' in body
    assert "lines already on a work order keep the rate they were logged at" in body


def test_saving_the_rates_from_the_panel(client, signed_in, ctx):
    user = signed_in("director")
    r = client.post("/settings/targets/", {**TARGETS, "labor_rate": "$90.50", "vendor_labor_rate": "1,250"}, **HX)
    assert r.status_code == 200 and toast_of(r) == "Targets and labor rates saved"
    s = fs.get_settings()
    assert s.labor_rate == Decimal("90.50") and s.vendor_labor_rate == Decimal("1250") and s.history.first().history_user == user
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="panel mt" id="set-targets">')
    assert 'name="labor_rate" value="90.5"' in body and 'name="vendor_labor_rate" value="1250"' in body


@pytest.mark.parametrize("field, typed, message", [("labor_rate", "82.555", "Use at most 2 decimal places."),
                                                   ("vendor_labor_rate", "-5", "Vendor labor rate must be between $0 and $9,999.99 an hour."),
                                                   ("labor_rate", "", "In-house labor rate is required."),
                                                   ("labor_rate", "1,5", "Enter a number.")])
def test_a_refused_rate_keeps_what_was_typed_and_saves_nothing(client, signed_in, ctx, field, typed, message):
    signed_in("director")
    post = {**TARGETS, "target_pm_pct": "97", "labor_rate": "90", "vendor_labor_rate": "250", field: typed}
    r = client.post("/settings/targets/", post, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and toast_of(r) == message and message in body
    assert f'name="{field}" value="{typed}"' in body and 'name="target_pm_pct" value="97"' in body
    assert re.search(rf'name="{field}" value="[^"]*"[^>]*aria-invalid="true"', body)
    s = fs.get_settings()
    assert s.target_pm_pct == Decimal("95.0") and s.labor_rate == Decimal("82.00") and s.vendor_labor_rate == Decimal("215.00")  # all or nothing


def test_a_post_without_the_rates_leaves_them_as_they_are(client, signed_in, ctx):
    fs.update_settings(labor_rate="90")
    signed_in("director")
    r = client.post("/settings/targets/", {**TARGETS, "target_pm_pct": "96"}, **HX)
    assert toast_of(r) == "Targets saved" and fs.get_settings().labor_rate == Decimal("90.00")
    r = client.post("/settings/targets/", {**TARGETS, "target_pm_pct": "40"}, **HX)  # refused: the rates show as saved
    assert 'name="labor_rate" value="90"' in r.content.decode()


@pytest.mark.parametrize("role, status", [("manager", 403), ("technician", 403), ("analyst", 403), ("requester", 403), ("vendor", 403)])
def test_changing_the_rates_needs_settings_edit(client, signed_in, ctx, role, status):
    signed_in(role)
    assert client.post("/settings/targets/", {**TARGETS, "labor_rate": "1"}, **HX).status_code == status
    assert fs.get_settings().labor_rate == Decimal("82.00")


def test_settings_viewers_see_the_rates_read_only(client, signed_in, ctx):
    signed_in("manager")  # Settings View
    body = client.get("/settings/").content.decode()
    assert re.search(r'name="labor_rate" value="82"[^>]*disabled', body) and "Save targets and rates" not in body
