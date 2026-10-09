"""
Slice 29, wave 2 A: temporary equipment (rentals, vendor loaners, demo units) on the web screens.

- Add rental or loaner (views_temporary.temporary_new): Equipment Edit; whose it is, the owner, the reference, the dates, the device of
  ours a vendor loaner stands in for, and how it arrives (new waiting, new inspected now, already on site); the services' refusals on
  their fields with the values kept; the serial warning (never a refusal); "Vendor loaner arrived" prefilled from a device of ours.
- The drawer's temporary section (asset_tabs.temporary_box) and its actions: Change details and Return to owner (Equipment Edit; the
  work that blocks a return named up front; cleaning and patient data), Keep it (Equipment Approve), "Return the loaner" once the device
  of ours is back; a scoped user's masked stands-in device; the sticker, the PM and Costs tabs.
- The Equipment list: Whose, the Status option "Returned to owner", the table's cells, the summary's "N temporary on site", the CSV.
- The Overview strip's segment, the completion modal's rental checklist and the owner's PM refusal up front, the recall batch's toast,
  the contract guards, and New work order with no PM for a temporary device.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from csvutil import csv_rows
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, create_default_roles
from apps.contracts.models import Contract
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData
from apps.tenants.context import tenant_context
from apps.web import asset_tabs
from apps.web.overview import fleet_strip
from apps.web.templatetags.web import pm_sticker
from apps.web.views_exports import EQUIPMENT_COLUMNS
from apps.web.views_recalls import batch_message
from apps.workorders import inspections
from apps.workorders.completion import checklist_of, checklist_signature, procedure_for
from apps.workorders.models import WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
NEW_URL = "/equipment/new/temporary/"
S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, v in fields.items():
            setattr(user, k, v)
        if fields:
            user.save()
        client.force_login(user)
        return user

    return _as


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def toast_of(r) -> str:
    return triggers(r)["toast"]["value"]


def field_error(body: str, field: str, prefix: str = "dv") -> str:
    marker = f'id="{prefix}-{field}_error">'
    return body.split(marker, 1)[1].split("</span>", 1)[0] if marker in body else ""


def rental(model, dept, tag="T-0001", **kw):
    """A rental added new, waiting for its incoming inspection, through the service."""
    today = timezone.localdate()
    values = {"tag": tag, "device_model": model, "department": dept, "kind": Ownership.RENTAL, "owner": "Acme Rentals", "serial": "SN-1",
              "owner_reference": "RA-2026-0042", "due_back_on": today + timedelta(days=14), "owner_pm_due_on": today + timedelta(days=60)}
    return eq.add_temporary_device(**{**values, **kw})


def on_site(model, dept, tag="T-0002", **kw):
    """A temporary device already on site when entered: in service, no inspection."""
    today = timezone.localdate()
    kw.setdefault("arrived_on", today - timedelta(days=10))
    return rental(model, dept, tag, added_as=AddedAs.EXISTING, **kw)


def add_post(dm, dept, **over) -> dict:
    """Add rental or loaner's post: a new rental, waiting for its incoming inspection, unless `over` says otherwise."""
    today = timezone.localdate()
    data = {"kind": Ownership.RENTAL, "tag": "T-0100", "serial": "BED-77", "device_model": str(dm.pk), "department": str(dept.pk), "room": "4",
            "owner": "Acme Rentals", "owner_reference": "RA-2026-0100", "stands_in_for": "", "arrived_on": today.isoformat(),
            "due_back_on": (today + timedelta(days=14)).isoformat(), "owner_pm_due_on": (today + timedelta(days=90)).isoformat(),
            "intake": "waiting", "inspection_due": "", "risk_class": "medium", "oem_pm_interval_months": "12", "expected_life_years": "8",
            "list_cost": "", "manufacturer": "", "model": "", "description": "", "category": "", "new_department": ""}
    data.update(over)
    return data


def drawer(client, tag, tab="overview") -> str:
    r = client.get(f"/equipment/{tag}/?tab={tab}", **HX)
    assert r.status_code == 200
    return r.content.decode()


def steps_data(wo) -> dict:
    steps = checklist_of(procedure_for(wo))
    data = {"signature": checklist_signature(steps), "resolution": "Checked on arrival"}
    for n, (_text, measure) in enumerate(steps, 1):
        data[f"step_{n}"] = "pass"
        data[f"reading_{n}"] = "42" if measure is not None else ""
    return data


@pytest.fixture
def or_dept(ctx):
    return Department.objects.create(name="OR")


# --- the doors' levels ------------------------------------------------------------------------------------------------------------

def test_each_door_at_its_level_and_never_for_a_scoped_user(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    doors = [NEW_URL, f"{NEW_URL}match/", f"/equipment/{a.tag}/stay/", f"/equipment/{a.tag}/return/"]
    signed_in("analyst")  # Equipment View
    for url in doors + [f"/equipment/{a.tag}/keep/"]:
        assert client.get(url, **HX).status_code == 403, url
        assert client.post(url, {}, **HX).status_code == 403, url
    for who, fields in (("requester", {"department": "ICU"}), ("vendor", {"company": "Acme Rentals"})):
        signed_in(who, **fields)
        for url in doors + [f"/equipment/{a.tag}/keep/"]:
            assert client.get(url, **HX).status_code == 403, url
    signed_in("technician")  # Equipment Edit: adds, changes, returns; never keeps
    for url in doors:
        assert client.get(url, **HX).status_code == 200, url
    assert client.get(f"/equipment/{a.tag}/keep/", **HX).status_code == 403
    assert client.post(f"/equipment/{a.tag}/keep/", {"acquisition_cost": "100"}, **HX).status_code == 403
    signed_in("director")  # Approve keeps
    assert client.get(f"/equipment/{a.tag}/keep/", **HX).status_code == 200
    a.refresh_from_db()
    assert a.ownership == Ownership.RENTAL


def test_another_facilitys_device_is_never_reached(client, signed_in, tenant, other_tenant, today, pump_model, dept):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        their_model = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        theirs = rental(their_model, their_dept, "THEIRS-1", added_as=AddedAs.EXISTING, arrived_on=today - timedelta(days=1))
        ours_there = Asset.objects.create(tag="CE-THEIRS", device_model=their_model, department=their_dept)
    signed_in("director")
    for url in (f"/equipment/{theirs.tag}/stay/", f"/equipment/{theirs.tag}/return/", f"/equipment/{theirs.tag}/keep/"):
        assert client.get(url, **HX).status_code == 404, url
        assert client.post(url, {"cleaning": "labeled", "data": "cleared", "acquisition_cost": "1"}, **HX).status_code == 404, url
    body = client.get(NEW_URL, {"for": ours_there.tag}, **HX).content.decode()  # fills in nothing from another facility
    assert "A vendor loaner standing in for" not in body and "CE-THEIRS" not in body
    body = client.post(NEW_URL, add_post(pump_model, dept, kind="loaner", stands_in_for="CE-THEIRS"), **HX).content.decode()
    assert field_error(body, "stands_in_for") == "No device here has the tag CE-THEIRS." and not Asset.objects.filter(tag="T-0100").exists()
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.status == S.IN_SERVICE and theirs.ownership == Ownership.RENTAL


# --- Add rental or loaner ---------------------------------------------------------------------------------------------------------

def test_a_new_rental_is_added_waiting_with_its_inspection_opened(client, signed_in, today, pump_model, dept):
    signed_in("manager")  # Equipment Edit, Work orders Approve: the inspector select
    body = client.get(NEW_URL, **HX).content.decode()
    assert "Add rental or loaner" in body and "Ours (owned, leased, or placed: on our PM program)" in body
    assert 'name="installed_on"' not in body and 'name="acquisition_cost"' not in body and 'name="notes"' not in body
    assert "Rental agreement, PO, or RMA number" in body and "Never a patient&#x27;s name or record number." in body
    assert re.search(r'<input type="radio" name="kind" value="rental"[^>]*checked', body)
    assert 'name="inspector"' in body and 'list="tmp-owners"' in body and "<option value=\"BD\">" in body  # the manufacturers are offered
    r = client.post(NEW_URL, add_post(pump_model, dept), **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    a = Asset.objects.get(tag="T-0100")
    assert (a.ownership, a.owner, a.owner_reference, a.serial, a.arrived_on) == (Ownership.RENTAL, "Acme Rentals", "RA-2026-0100", "BED-77", today)
    assert (a.status, a.awaiting_inspection, a.next_pm_on, a.acquisition_cost) == (S.OUT_OF_SERVICE, True, None, Decimal("0"))
    wo = inspections.open_inspection(a)
    assert toast_of(r) == f"T-0100 added; {wo.number} opened for its incoming inspection"
    events = triggers(r)
    assert "devices-changed" in events and "wo-changed" in events and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    body = r.content.decode()
    assert "Rental · Acme Rentals" in body and "its owner maintains it" in body and "RA-2026-0100" in body


def test_one_already_on_site_is_in_service_and_its_arrival_day_is_required(client, signed_in, today, pump_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, add_post(pump_model, dept, intake="existing", arrived_on=""), **HX)
    body = r.content.decode()
    assert field_error(body, "arrived_on") == "Enter the day it arrived." and not Asset.objects.exists()
    assert 'value="T-0100"' in body and 'value="BED-77"' in body and 'value="RA-2026-0100"' in body  # what was typed is kept
    arrived = today - timedelta(days=5)
    r = client.post(NEW_URL, add_post(pump_model, dept, intake="existing", arrived_on=arrived.isoformat()), **HX)
    a = Asset.objects.get(tag="T-0100")
    assert (a.status, a.added_as, a.arrived_on, a.awaiting_inspection) == (S.IN_SERVICE, AddedAs.EXISTING, arrived, False)
    assert inspections.open_inspection(a) is None and toast_of(r) == "T-0100 added"


def test_the_services_refusals_come_back_on_their_fields_with_the_values_kept(client, signed_in, today, pump_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, add_post(pump_model, dept, owner=" ", serial="", owner_reference="RA 2026 1",
                                      due_back_on=(today - timedelta(days=1)).isoformat()), **HX)
    body = r.content.decode()
    assert field_error(body, "owner") == "Enter the company that owns it."
    assert field_error(body, "serial") == "Enter the serial number on the unit."
    assert field_error(body, "owner_reference") == ("Enter the agreement, PO, or RMA number as the owner&#x27;s paperwork shows it, without "
                                                    "spaces.")
    assert field_error(body, "due_back_on") == f"It cannot be due back before it arrived ({day(today)})."
    assert 'value="RA 2026 1"' in body and 'value="T-0100"' in body and not Asset.objects.exists()
    assert body.count("autofocus") == 1 and re.search(r'name="serial"[^>]*autofocus', body)  # the first field with an error, as laid out
    r = client.post(NEW_URL, add_post(pump_model, dept, kind="owned"), **HX)
    assert field_error(r.content.decode(), "kind") == "Choose rental, vendor loaner, or demo unit." and not Asset.objects.exists()


def test_a_vendor_loaner_stands_in_for_a_device_of_ours_by_its_tag(client, signed_in, today, pump_model, dept, pump):
    signed_in("technician")
    r = client.post(NEW_URL, add_post(pump_model, dept, kind="loaner", owner="BD", stands_in_for="ce-10002"), **HX)
    loaner = Asset.objects.get(tag="T-0100")
    assert loaner.ownership == Ownership.LOANER and loaner.stands_in_for == pump and r["HX-Retarget"] == "#drawer"
    body = client.post(NEW_URL, add_post(pump_model, dept, tag="T-0101", kind="loaner", stands_in_for="CE-404"), **HX).content.decode()
    assert field_error(body, "stands_in_for") == "No device here has the tag CE-404."
    body = client.post(NEW_URL, add_post(pump_model, dept, tag="T-0101", kind="loaner", stands_in_for="T-0100"), **HX).content.decode()
    assert field_error(body, "stands_in_for") == "T-0100 is not ours: a loaner stands in for a device of ours."
    # a rental ignores the hidden stands-in box, whatever the browser kept in it
    client.post(NEW_URL, add_post(pump_model, dept, tag="T-0102", stands_in_for="CE-404"), **HX)
    assert Asset.objects.get(tag="T-0102").stands_in_for is None


def test_a_unit_here_before_is_warned_about_never_refused(client, signed_in, today, pump_model, dept):
    first = on_site(pump_model, dept, "T-0012", serial="BED-77")
    eq.return_to_owner(first, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED, today=today)
    signed_in("technician")
    body = client.get(f"{NEW_URL}match/", {"device_model": str(pump_model.pk), "serial": "bed-77"}, **HX).content.decode()
    assert f"This unit was here before as T-0012, returned {day(today)}. Each stay is its own record" in body
    assert "note warn" not in client.get(f"{NEW_URL}match/", {"device_model": str(pump_model.pk), "serial": "BED-78"}, **HX).content.decode()
    r = client.post(NEW_URL, add_post(pump_model, dept), **HX)
    assert Asset.objects.filter(tag="T-0100").exists()
    assert toast_of(r).endswith(f"; this unit was here before as T-0012, returned {day(today)}")


def test_inspect_it_now_assigns_the_inspection_and_opens_its_rental_checklist(client, signed_in, today, pump_model, dept):
    user = signed_in("technician")
    tech = Technician.objects.create(user=user, name="Dana Whitfield")
    Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Infusion pumps")
    r = client.post(NEW_URL, add_post(pump_model, dept, intake="inspect"), **HX)
    a = Asset.objects.get(tag="T-0100")
    wo = inspections.open_inspection(a)
    assert wo.assigned_to == tech and json.loads(r["HX-Location"])["path"] == f"/work-orders/{wo.number}/complete/"
    assert toast_of(r) == f"T-0100 added; record its incoming inspection, {wo.number}"
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    assert "this is the rental checklist, the incoming checklist with the owner's PM label" in body
    assert "Owner&#x27;s PM label current" in body and "PMs start" not in body
    assert "Goes into service; its owner maintains it" in body
    r = client.post(f"/work-orders/{wo.number}/complete/", {**steps_data(wo), "inspection_result": "passed"}, **HX)
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.IN_SERVICE, False, None)
    assert toast_of(r) == f"{wo.number} completed: Incoming inspection passed; T-0100 in service"


def test_the_owners_past_pm_date_is_said_before_the_inspection_is_filled_in(client, signed_in, today, pump_model, dept, techs):
    a = rental(pump_model, dept, owner_pm_due_on=today - timedelta(days=3))
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    signed_in("technician")
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    refusal = f"The owner&#x27;s PM was due {day(today - timedelta(days=3))}: have the owner do it, or record its new date."
    assert refusal in body and "Not until the owner&#x27;s PM is done, or its new date recorded" in body
    body = client.post(f"/work-orders/{wo.number}/complete/", {**steps_data(wo), "inspection_result": "passed"}, **HX).content.decode()
    assert refusal in body and inspections.open_inspection(a) == wo  # refused, still open


# --- Vendor loaner arrived, and Return the loaner ---------------------------------------------------------------------------------

def test_vendor_loaner_arrived_fills_in_the_add_modal_from_a_device_of_ours(client, signed_in, today, pump_model, dept, pump):
    pump.room = "12"
    pump.save()
    signed_in("technician")
    assert "Vendor loaner arrived" not in drawer(client, pump.tag)  # in service, no vendor repair
    wo = create_work_order(asset=pump, type=WoType.REPAIR, priority="normal", problem="Keypad", tag_out=True)
    assign(wo, vendor_name="BD Field Service Team")
    body = drawer(client, pump.tag)
    assert f'hx-get="/equipment/new/temporary/?for={pump.tag}"' in body and "Vendor loaner arrived" in body
    body = client.get(NEW_URL, {"for": pump.tag}, **HX).content.decode()
    assert re.search(r'<input type="radio" name="kind" value="loaner"[^>]*checked', body)
    assert f'<option value="{pump_model.pk}" selected>' in body and f'<option value="{dept.pk}" selected>' in body
    assert 'name="room" value="12"' in body and f'name="stands_in_for" value="{pump.tag}"' in body
    assert 'name="owner" value="BD Field Service Team"' in body and f"A vendor loaner standing in for {pump.tag}" in body
    signed_in("analyst")
    assert "Vendor loaner arrived" not in drawer(client, pump.tag)


def test_once_our_device_is_back_both_drawers_offer_to_return_the_loaner(client, signed_in, today, pump_model, dept, pump):
    loaner = on_site(pump_model, dept, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    eq.set_status(pump, S.OUT_OF_SERVICE)
    signed_in("technician")
    body = drawer(client, pump.tag)
    assert "Vendor loaner <a" in body and f">{loaner.tag}</a> from BD stands in for {pump.tag}" in body and "Return the loaner" not in body
    assert "Vendor loaner arrived" not in body  # one stands in already
    assert "Return the loaner" not in drawer(client, loaner.tag)
    eq.set_status(pump, S.IN_SERVICE)
    body = drawer(client, pump.tag)
    assert f"stands in for {pump.tag}, which is back in service: return the loaner." in body
    assert f'hx-get="/equipment/{loaner.tag}/return/?back={pump.tag}"' in body
    body = drawer(client, loaner.tag)
    assert f"{pump.tag}, the device this loaner stands in for, is back in service: return the loaner to BD." in body
    # returned from our device's drawer: the answer is that drawer again
    form = client.get(f"/equipment/{loaner.tag}/return/?back={pump.tag}", **HX).content.decode()
    assert f'name="back" value="{pump.tag}"' in form
    r = client.post(f"/equipment/{loaner.tag}/return/", {"back": pump.tag, "on": today.isoformat(), "cleaning": "decontaminated",
                                                         "data": "none_stored"}, **HX)
    assert r["HX-Retarget"] == "#drawer" and f"<h2>{pump.tag} · " in r.content.decode() and toast_of(r) == f"{loaner.tag} returned to BD"
    assert "stands in for" not in drawer(client, pump.tag)


# --- the drawer ---------------------------------------------------------------------------------------------------------------------

def test_the_drawer_shows_the_stay_in_place_of_our_pm_and_offers_each_action_at_its_level(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    signed_in("technician")
    body = drawer(client, a.tag)
    assert "Rental · Acme Rentals" in body and "RA-2026-0042" in body and day(today - timedelta(days=10)) in body
    assert "(10 d on site)" in body and "(in 14 d)" in body and f"Owner&#x27;s PM {(today + timedelta(days=60)):%b %Y}" in body
    for gone in ("Who can service this", "<dt>PM interval</dt>", "<dt>Next PM</dt>", "<dt>Acquisition cost</dt>", "<dt>Contract</dt>"):
        assert gone not in body, gone
    assert "Change details" in body and "Return to owner" in body and "Keep it" not in body
    assert f'hx-get="/contracts/assets/{a.tag}/support/"' not in body  # no support editor: its owner maintains it
    signed_in("director")
    assert "Keep it" in drawer(client, a.tag)
    signed_in("analyst")
    body = drawer(client, a.tag)
    assert "Rental · Acme Rentals" in body and "Change details" not in body and "Return to owner" not in body and "Keep it" not in body
    # the full page carries the same section
    page = client.get(f"/equipment/{a.tag}/").content.decode()
    assert "<!doctype html>" in page.lower() and "Rental · Acme Rentals" in page and "RA-2026-0042" in page


def test_a_past_due_back_and_a_past_owners_pm_read_as_past(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept, due_back_on=today - timedelta(days=3), owner_pm_due_on=today - timedelta(days=1))
    signed_in("technician")
    body = drawer(client, a.tag)
    assert "(past due 3 d)" in body and "(past: the owner does it, or record its new date)" in body
    assert 'class="stk over"' in body


def test_a_scoped_user_never_sees_the_device_of_ours_a_loaner_stands_in_for(client, signed_in, today, pump_model, dept, or_dept):
    ours = Asset.objects.create(tag="CE-OR-1", device_model=pump_model, department=or_dept, next_pm_on=today + timedelta(days=90))
    loaner = on_site(pump_model, dept, kind=Ownership.LOANER, owner="BD", stands_in_for=ours)
    signed_in("requester", department="ICU")
    body = drawer(client, loaner.tag)
    assert "A device of ours" in body and "CE-OR-1" not in body
    assert "Change details" not in body and "Return to owner" not in body
    signed_in("technician")
    assert ">CE-OR-1</a>" in drawer(client, loaner.tag)


def test_the_sticker_says_the_owners_pm_date(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, owner_pm_due_on=today + timedelta(days=60))
    assert f"Owner&#x27;s PM {(today + timedelta(days=60)):%b %Y}" in pm_sticker(a) and "stk own" in pm_sticker(a)
    eq.update_temporary(a, owner_pm_due_on=today - timedelta(days=1))
    assert "stk over" in pm_sticker(a)
    eq.update_temporary(a, owner_pm_due_on=None)
    assert ">Owner maintains<" in pm_sticker(a)
    eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED)
    assert ">Returned<" in pm_sticker(a)


def test_the_pm_tab_shows_the_owners_date_and_the_rental_checklist_and_costs_no_outlook(client, signed_in, today, pump_model, dept):
    a = rental(pump_model, dept)
    signed_in("technician")
    body = drawer(client, a.tag, "pm")
    assert "Its owner, Acme Rentals, maintains it" in body and day(today + timedelta(days=60)) in body
    assert "Owner&#x27;s PM label current" in body and "Projected" not in body and "1 incoming inspection" in body
    body = drawer(client, a.tag, "costs")
    assert "Replacement outlook" not in body and "Not ours: no acquisition cost and no contract share" in body
    assert asset_tabs.replacement_outlook(a, today)["score_pct"] is None


# --- Change details ---------------------------------------------------------------------------------------------------------------

def test_change_details_saves_the_stay_and_keeps_what_was_typed_on_a_refusal(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    signed_in("technician")
    body = client.get(f"/equipment/{a.tag}/stay/", **HX).content.decode()
    assert 'name="owner" value="Acme Rentals"' in body and 'name="stands_in_for"' not in body  # a rental stands in for nothing
    post = {"owner": "Acme Rentals", "owner_reference": "RA-2026-0042", "due_back_on": (today - timedelta(days=11)).isoformat(),
            "owner_pm_due_on": (today + timedelta(days=200)).isoformat()}
    body = client.post(f"/equipment/{a.tag}/stay/", post, **HX).content.decode()
    assert field_error(body, "due_back_on", "st") == f"It cannot be due back before it arrived ({day(today - timedelta(days=10))})."
    assert f'value="{(today + timedelta(days=200)).isoformat()}"' in body
    r = client.post(f"/equipment/{a.tag}/stay/", {**post, "owner": "Acme Medical Rentals", "due_back_on": ""}, **HX)
    a.refresh_from_db()
    assert (a.owner, a.due_back_on, a.owner_pm_due_on) == ("Acme Medical Rentals", None, today + timedelta(days=200))
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{a.tag}: stay details saved"
    eq.return_to_owner(a, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NOT_APPLICABLE)
    body = client.get(f"/equipment/{a.tag}/stay/", **HX).content.decode()
    assert f"{a.tag} was returned to its owner on {day(today)}." in body and "<form" not in body


# --- Return to owner ----------------------------------------------------------------------------------------------------------------

def test_return_to_owner_names_blocking_work_up_front_then_records_the_return(client, signed_in, today, pump_model, dept):
    a = rental(pump_model, dept)  # waiting: its open incoming inspection is cancelled with the return
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Rail")
    signed_in("technician")
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()
    assert f"{a.tag} has open work: {repair.number} (corrective repair, open)." in body and "<form" not in body
    change_status(repair, WoStatus.CANCELLED)
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()
    inspection = inspections.open_inspection(a)
    assert f"{inspection.number} (incoming inspection) is cancelled with it" in body and 'name="cleaning"' in body
    body = client.post(f"/equipment/{a.tag}/return/", {"on": today.isoformat()}, **HX).content.decode()
    assert field_error(body, "cleaning", "rt") == "Say how it was cleaned before it left."
    assert field_error(body, "data", "rt") == "Say what was done about patient data on it."
    r = client.post(f"/equipment/{a.tag}/return/", {"on": today.isoformat(), "cleaning": "labeled", "data": "cleared"}, **HX)
    a.refresh_from_db()
    assert (a.status, a.returned_on, a.return_cleaning, a.return_data) == (S.RETIRED, today, "labeled", "cleared")
    assert toast_of(r) == f"{a.tag} returned to Acme Rentals; 1 open work order cancelled" and "wo-changed" in triggers(r)
    body = r.content.decode()
    assert "Returned to owner" in body and "Parts still contaminated are labeled" in body and "Patient data cleared" in body
    assert "Retire" not in body and "Return to owner</button>" not in body and ">Returned<" in body
    assert "Retired while still waiting" not in body  # the incoming banner says nothing of a returned unit
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()
    assert f"{a.tag} was returned to its owner on {day(today)}." in body


def test_a_held_or_missing_device_is_refused_up_front(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    eq.set_status(a, S.MISSING)
    signed_in("technician")
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()  # review fix: a missing unit leaves as not in hand only
    assert "Not in hand: lost, settled with its owner" in body and "Cleaned and decontaminated" not in body
    signed_in("director")
    assert f"{a.tag} is missing: mark it found before keeping it." in client.get(f"/equipment/{a.tag}/keep/", **HX).content.decode()


# --- Keep it ------------------------------------------------------------------------------------------------------------------------

def test_keep_it_makes_it_ours_at_approve(client, signed_in, today, pump_model, dept):
    waiting = rental(pump_model, dept)
    signed_in("director")
    body = client.get(f"/equipment/{waiting.tag}/keep/", **HX).content.decode()
    assert f"has not passed its incoming inspection ({inspections.open_inspection(waiting).number})" in body and "<form" not in body
    assert ">Keep it</button>" not in drawer(client, waiting.tag)  # not offered until it could be kept
    a = on_site(pump_model, dept)
    assert ">Keep it</button>" in drawer(client, a.tag)
    body = client.post(f"/equipment/{a.tag}/keep/", {"acquisition_cost": "", "next_pm_on": today.isoformat(), "warranty_end": ""}, **HX).content.decode()
    assert field_error(body, "acquisition_cost", "kp") == "Enter what the facility paid for it (0 if nothing)."
    r = client.post(f"/equipment/{a.tag}/keep/", {"acquisition_cost": "1800", "next_pm_on": "", "warranty_end": ""}, **HX)
    a.refresh_from_db()
    assert (a.ownership, a.kept_on, a.acquisition_cost, a.next_pm_on) == (Ownership.OWNED, today, Decimal("1800.00"), today)
    assert toast_of(r) == f"{a.tag} kept by the facility: ours from today, first PM due {day(today)}"
    body = r.content.decode()
    assert f"Kept by the facility on {today:%b} {today.day}, {today.year}: it came from Acme Rentals (RA-2026-0042)" in body
    assert "Who can service this" in body and "Change details" not in body


# --- Edit details, New work order, contracts --------------------------------------------------------------------------------------

def test_edit_details_of_a_temporary_device_has_none_of_our_fields(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    signed_in("technician")
    body = client.get(f"/equipment/{a.tag}/edit/", **HX).content.decode()
    for name in ("installed_on", "warranty_end", "acquisition_cost", "next_pm_on", "notes"):
        assert f'name="{name}"' not in body, name
    assert "Its owner maintains it" in body
    r = client.post(f"/equipment/{a.tag}/edit/", {"serial": "SN-1", "device_model": str(pump_model.pk), "department": str(dept.pk),
                                                  "room": "7", "condition": "4"}, **HX)
    a.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and (a.room, a.condition, a.acquisition_cost) == ("7", 4, Decimal("0"))


def test_new_work_order_offers_no_pm_for_a_temporary_device(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    signed_in("manager")
    body = client.get("/work-orders/new/", {"asset": a.tag}, **HX).content.decode()
    assert '<option value="pm"' not in body and '<option value="repair"' in body
    assert "Vendor service (Acme Rentals)" in body  # vendor service names its owner
    body = client.post("/work-orders/new/", {"asset": a.tag, "type": "pm", "priority": "normal", "problem": "PM"}, **HX).content.decode()
    assert f"{a.tag} is a rental: its owner maintains it, so it gets no PM work orders here." in body
    assert not WorkOrder.objects.filter(asset=a).exists()


def test_the_contract_doors_leave_a_temporary_device_out(client, signed_in, today, pump_model, dept):
    a = on_site(pump_model, dept)
    signed_in("director")
    refusal = f"{a.tag} is a rental: its owner maintains it, so it goes on no service contract here."
    body = client.get("/contracts/new/", {"asset": a.tag}, **HX).content.decode()
    assert refusal in body and 'name="asset"' not in body
    post = {"asset": a.tag, "reference": "SC-9", "vendor": "BioServ", "type": "oem", "coverage": "full", "start_on": today.isoformat(),
            "end_on": (today + timedelta(days=365)).isoformat(), "annual_cost": "100", "notes": ""}
    r = client.post("/contracts/new/", post, **HX)
    assert r.status_code == 200 and Contract.objects.filter(reference="SC-9").exists()
    a.refresh_from_db()
    assert a.contract_id is None
    body = client.get(f"/contracts/assets/{a.tag}/support/", **HX).content.decode()
    assert refusal in body and 'name="contract"' not in body and "New contract for this device" not in body
    contract = Contract.objects.get(reference="SC-9")
    r = client.post(f"/contracts/assets/{a.tag}/support/", {"contract": str(contract.pk)}, **HX)
    a.refresh_from_db()
    assert toast_of(r) == refusal and a.contract_id is None


# --- the Equipment list, its summary, and its CSV ---------------------------------------------------------------------------------

@pytest.fixture
def mixed(ctx, today, pump_model, dept, pump, vent):
    """Ours (pump, vent; vent retired), a rental on site, a demo unit past due back, and a returned rental."""
    eq.set_status(vent, S.RETIRED)
    out = {"rental": on_site(pump_model, dept, "T-1"),
           "demo": on_site(pump_model, dept, "T-2", kind=Ownership.DEMO, owner="Mindray", due_back_on=today - timedelta(days=2)),
           "returned": on_site(pump_model, dept, "T-3")}
    eq.return_to_owner(out["returned"], cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)
    return out


def listed(client, **params) -> set[str]:
    r = client.get("/equipment/", params)
    return {a.tag for a in r.context["page"]}


def test_whose_and_returned_to_owner_filter_the_list(client, signed_in, mixed, pump, vent):
    signed_in("analyst")
    assert listed(client) == {pump.tag, vent.tag, "T-1", "T-2", "T-3"}
    assert listed(client, whose="ours") == {pump.tag, vent.tag}
    assert listed(client, whose="temporary") == {"T-1", "T-2"}
    assert listed(client, whose="rental") == {"T-1", "T-3"}
    assert listed(client, whose="demo") == {"T-2"}
    assert listed(client, whose="past_due") == {"T-2"}
    assert listed(client, status="returned") == {"T-3"}
    assert listed(client, status="retired") == {vent.tag}  # retired means ours
    assert listed(client, whose="rental", status="returned") == {"T-3"}
    assert listed(client, whose="nonsense") == listed(client)


def test_the_table_and_summary_read_whose_and_how_long(client, signed_in, today, mixed):
    signed_in("analyst")
    page = client.get("/equipment/").content.decode()
    assert '<option value="returned">Returned to owner</option>' in page and '<option value="temporary">Temporary on site</option>' in page
    assert 'href="/equipment/?whose=temporary"' in page and ">2 temporary on site</a>" in page
    assert "Rental · Acme Rentals" in page and "Demo or evaluation unit · Mindray" in page
    assert "Past due back 2 d" in page and "10 d on site" in page and f"Returned {today:%b} {today.day}" in page
    assert "Due back " in page and "Back with its owner" in page
    # the table's own re-fetch brings the summary along
    r = client.get("/equipment/", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="eq-table")
    assert ">2 temporary on site</a>" in r.content.decode() and r.context["temporary_on_site"] == 2


def test_the_csv_carries_whose_and_the_stay(client, signed_in, today, mixed, pump):
    signed_in("analyst")
    rows = csv_rows(client.get("/export/equipment.csv"))
    assert rows[0] == EQUIPMENT_COLUMNS and EQUIPMENT_COLUMNS[-5:] == ["Whose", "Owner", "Reference", "Arrived", "Due back"]
    by_tag = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert {k: by_tag["T-2"][k] for k in ("Whose", "Owner", "Reference", "Arrived", "Due back", "Status", "Support", "Fleet state")} == {
        "Whose": "Demo or evaluation unit", "Owner": "Mindray", "Reference": "RA-2026-0042", "Arrived": (today - timedelta(days=10)).isoformat(),
        "Due back": (today - timedelta(days=2)).isoformat(), "Status": "In service", "Support": "Owner maintains", "Fleet state": "Temporary on site"}
    assert by_tag["T-3"]["Status"] == "Returned to owner" and by_tag["T-3"]["Fleet state"] == "Returned to owner"
    assert {k: by_tag[pump.tag][k] for k in ("Whose", "Owner", "Reference", "Arrived", "Due back")} == {
        "Whose": "Ours", "Owner": "", "Reference": "", "Arrived": "", "Due back": ""}
    rows = csv_rows(client.get("/export/equipment.csv", {"whose": "temporary"}))
    assert {r[0] for r in rows[1:]} == {"T-1", "T-2"}


def test_the_overview_strip_counts_temporary_devices_on_site_apart(ctx, mixed):
    strip = fleet_strip(eq.fleet_bucket_counts())
    temporary = next(s for s in strip["segments"] if s["key"] == "temporary")
    assert temporary["n"] == 2 and temporary["label"] == "Temporary on site" and temporary["url"].endswith("?bucket=temporary")
    assert strip["retired"] == 1  # the returned rental is not "retired, awaiting disposition"
    assert fleet_strip({"compliant": 3, "pm_due": 1, "pm_overdue": 0, "open_recall": 0, "in_repair": 0, "out_of_service": 0,
                        "retired": 0})["active"] == 4  # a bucket count without the slice 29 keys still works


def test_the_overview_legend_leaves_out_an_empty_temporary_segment(client, signed_in, ctx, pump):
    signed_in("director")
    body = client.get("/").content.decode()
    assert "Temporary on site" not in body


# --- Add device's Whose ------------------------------------------------------------------------------------------------------------

def test_add_device_offers_add_rental_or_loaner_first(client, signed_in):
    signed_in("technician")
    body = client.get("/equipment/new/", **HX).content.decode()
    assert "Ours (owned, leased, or placed: on our PM program)" in body
    assert 'hx-get="/equipment/new/temporary/?kind=rental"' in body
    page = client.get("/equipment/").content.decode()
    assert 'hx-get="/equipment/new/temporary/"' in page and "Add rental or loaner" in page
    body = client.get(NEW_URL, {"kind": "demo"}, **HX).content.decode()
    assert re.search(r'<input type="radio" name="kind" value="demo"[^>]*checked', body)
    signed_in("analyst")
    assert "Add rental or loaner" not in client.get("/equipment/").content.decode()


# --- the recall batch's toast -----------------------------------------------------------------------------------------------------

def test_the_recall_batch_counts_work_orders_sent_to_owners(client, signed_in, today, pump_model, dept, pump, pump_recall):
    rental_pump = on_site(pump_model, dept)
    role = Role.objects.get(slug="manager")
    assert role.level_for(Module.RECALLS) >= Level.APPROVE
    signed_in("manager")
    r = client.post(f"/recalls/{pump_recall.pk}/work-orders/", **HX)
    assert toast_of(r) == ("2 recall work orders created: 1 sent to its owner as vendor service, 1 left unassigned (no credentialed "
                           "technician)")
    wo = WorkOrder.objects.get(asset=rental_pump, type=WoType.RECALL)
    assert wo.vendor_service and wo.vendor_name == "Acme Rentals"
    assert batch_message(3, 0) == "3 recall work orders created and assigned to credentialed technicians"
    assert batch_message(1, 1) == "1 recall work order created; no credentialed technician, left unassigned"
    assert batch_message(3, 0, 2) == "3 recall work orders created: 2 sent to their owners as vendor service, 1 assigned to credentialed technicians"
    assert batch_message(0, 0) == "Every affected device already has a recall work order"
