"""
Slice 26, wave 2 B1: incoming inspections on the equipment screens.

- Add device asks how the device arrives (intake): new and waiting for its incoming inspection (the default; its due date; "Assign the
  inspection to me" through services.take for whoever may take work, or an inspector through services.assign for Work orders Approve),
  new and inspected now (the inspection assigned to the user, then its Mark completed), or already in use here (in or out of service,
  its last and next PM, its install date required). Hidden choices' values are dropped; the toast names the inspection and wo-changed
  fires.
- Edit details on a waiting device shows its next PM as "After its incoming inspection" and never sends one.
- The drawer's banner (inspections.banner's words, numbers linked for Work orders View and masked outside a scoped user's share; "Open
  incoming inspection" when none is open), "Put in use before inspection" (Equipment Approve) and its modal, the status chip
  ("Awaiting inspection"), the PM sticker ("After inspection"), the Next PM row, and the status toasts of a waiting device.
- The Equipment table and CSV word the status as status_label does; the PM tab lists the incoming inspection with the PMs and has no
  upcoming PMs while the device waits.
"""
import json
import re
from datetime import timedelta

import pytest
from csvutil import csv_rows
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, failed_device, in_use_device, passed_device, waiting_device

from apps.accounts.models import Level, Module, Role, User
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, RiskClass, UseBeforeInspection
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.web import asset_tabs
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import assign, change_status

HX = {"HTTP_HX_REQUEST": "true"}
NEW_URL = "/equipment/new/"
S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def toast_of(r) -> str:
    return triggers(r)["toast"]["value"]


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def field_error(body: str, field: str) -> str:
    marker = f'id="dv-{field}_error">'
    return body.split(marker, 1)[1].split("</span>", 1)[0] if marker in body else ""


def new_post(dm, dept, **over) -> dict:
    """Add device's post: new and waiting for its incoming inspection unless `intake` says otherwise."""
    data = {"tag": "CE-30001", "serial": "SN-1", "device_model": str(dm.pk), "department": str(dept.pk), "room": "4", "installed_on": "",
            "acquisition_cost": "", "warranty_end": "", "condition": "3", "intake": "waiting", "inspection_due": "", "last_pm_on": "",
            "next_pm_on": "", "status": S.IN_SERVICE, "notes": "", "risk_class": RiskClass.MEDIUM, "oem_pm_interval_months": "12",
            "expected_life_years": "8", "list_cost": "", "manufacturer": "", "model": "", "description": "", "category": "", "new_department": ""}
    data.update(over)
    return data


@pytest.fixture
def staff(ctx, make_user):
    """Dana, a technician with her own account and profile, credentialed for the Hamilton-G5 (by model); Tom, a technician with an
    account and a profile and no credentials; Kim, a CE manager (Work orders Approve) with a profile of her own; the director, with none."""
    out = {}
    for key, slug, name in (("dana", "technician", "Dana Whitfield"), ("tom", "technician", "Tom Okafor"), ("kim", "manager", "Kim Alvarez")):
        user = make_user(slug, username=f"{key}@riverside.example")
        out[key] = user
        out[f"{key}_tech"] = Technician.objects.create(user=user, name=name)
    Credential.objects.create(technician=out["dana_tech"], scope=Scope.MODEL, value="Hamilton-G5", expires_on=timezone.localdate() + timedelta(days=400))
    out["director"] = make_user("director")
    return out


def radio(body: str, value: str):
    m = re.search(rf'<input type="radio" name="intake" value="{value}"[^>]*>', body)
    return m.group(0) if m else None


def added(tag="CE-30001") -> Asset:
    return Asset.objects.select_related("device_model").get(tag=tag)


# --- Add device: what each user is offered ------------------------------------------------------------------------------------------

def test_add_device_offers_each_user_their_own_way_with_the_inspection(client, staff, make_user, today):
    # Dana may take work: "Assign the inspection to me", ticked; "inspect it now"; no inspector select
    client.force_login(staff["dana"])
    body = client.get(NEW_URL, **HX).content.decode()
    assert "checked" in radio(body, "waiting") and radio(body, "inspect") and radio(body, "existing")
    assert re.search(r'<input type="checkbox" name="take_inspection"[^>]*checked', body) and 'name="inspector"' not in body
    assert f'name="inspection_due" value="{(today + timedelta(days=inspections.INSPECTION_DUE_DAYS)).isoformat()}"' in body
    assert f'min="{today.isoformat()}"' in body
    # Kim assigns (Work orders Approve): the inspector select, the facility's active technicians and vendor service; inspect it now as herself
    client.force_login(staff["kim"])
    body = client.get(NEW_URL, **HX).content.decode()
    assert 'name="take_inspection"' not in body and radio(body, "inspect")
    select = body.split('name="inspector"', 1)[1].split("</select>", 1)[0]
    assert '<option value="" selected>Leave unassigned</option>' in select and "Dana Whitfield" in select and "Kim Alvarez" in select
    assert "Vendor service (the manufacturer&#x27;s field service)" in select
    # The director assigns too, but has no technician profile to inspect it now as
    client.force_login(staff["director"])
    body = client.get(NEW_URL, **HX).content.decode()
    assert 'name="inspector"' in body and radio(body, "inspect") is None
    # A technician account with no profile here takes nothing: neither the box nor inspect it now
    client.force_login(make_user("technician"))
    body = client.get(NEW_URL, **HX).content.decode()
    assert 'name="take_inspection"' not in body and 'name="inspector"' not in body and radio(body, "inspect") is None
    assert radio(body, "waiting") and radio(body, "existing")


def test_with_taking_work_off_a_technician_gets_neither_the_box_nor_inspect_it_now(client, staff, vent_model, dept):
    fs.update_settings(technicians_take_work=False)
    client.force_login(staff["dana"])
    body = client.get(NEW_URL, **HX).content.decode()
    assert 'name="take_inspection"' not in body and radio(body, "inspect") is None
    # posted anyway: not one of the ways listed
    r = client.post(NEW_URL, new_post(vent_model, dept, intake="inspect"), **HX)
    assert field_error(r.content.decode(), "intake") == "Choose one of the ways listed." and not Asset.objects.exists()


def test_equipment_edit_with_work_orders_view_adds_a_waiting_device_and_gives_the_inspection_to_nobody(client, tenant, ctx, vent_model, dept,
                                                                                                       make_user):
    """A custom role that may add devices but not work on work orders: the device waits with its inspection unassigned, whatever it
    posts; the default roles without Equipment Edit (analyst, a vendor's technician) never reach Add device."""
    role = Role.objects.create(name="Receiving", slug="receiving")
    role.set_levels({Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    user = User.objects.create_user(username="dock@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
    Technician.objects.create(user=user, name="Dock Clerk")  # a profile, but no Work orders Edit: takes nothing
    client.force_login(user)
    body = client.get(NEW_URL, **HX).content.decode()
    assert radio(body, "waiting") and radio(body, "existing") and radio(body, "inspect") is None
    assert 'name="take_inspection"' not in body and 'name="inspector"' not in body
    client.post(NEW_URL, new_post(vent_model, dept, take_inspection="on", inspector="vendor"), **HX)
    wo = inspections.open_inspection(added())
    assert (wo.assigned_to, wo.vendor_service) == (None, False)
    for slug in ("analyst", "vendor"):
        client.force_login(make_user(slug))
        assert client.get(NEW_URL, **HX).status_code == 403
        assert client.post(NEW_URL, new_post(vent_model, dept, tag=f"CE-{slug}"), **HX).status_code == 403
    assert Asset.objects.count() == 1


@pytest.fixture
def setup(staff, vent_model, pump_model, dept):
    return {"vent_model": vent_model, "pump_model": pump_model, "dept": dept, **staff}


# --- Add device: new, waiting for its incoming inspection ---------------------------------------------------------------------------

def test_a_new_device_waits_with_its_inspection_opened_unassigned(client, setup, today):
    client.force_login(setup["dana"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], take_inspection=""), **HX)  # the box cleared
    a = added()
    wo = inspections.open_inspection(a)
    assert (a.status, a.awaiting_inspection, a.next_pm_on, a.last_pm_on, a.added_as) == (S.OUT_OF_SERVICE, True, None, None, AddedAs.NEW)
    assert (wo.type, wo.status, wo.assigned_to, wo.vendor_service) == (WoType.INSPECTION, WoStatus.OPEN, None, False)
    assert (wo.opened_on, wo.due_on) == (today, today + timedelta(days=inspections.INSPECTION_DUE_DAYS))
    assert r["HX-Retarget"] == "#drawer" and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    t = triggers(r)
    assert t["toast"]["value"] == f"CE-30001 added; {wo.number} opened for its incoming inspection"
    assert "devices-changed" in t and "wo-changed" in t
    body = r.content.decode()
    assert "Awaiting inspection" in body and "Waiting for its incoming inspection: " in body and wo.number in body


def test_its_inspection_due_date_as_given_and_never_before_today(client, setup, today):
    client.force_login(setup["dana"])
    due = today + timedelta(days=30)  # a vendor's install
    client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], inspection_due=due.isoformat(), take_inspection=""), **HX)
    assert inspections.open_inspection(added()).due_on == due
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], tag="CE-30002", inspection_due=(today - timedelta(days=1)).isoformat()), **HX)
    assert field_error(r.content.decode(), "inspection_due") == "The inspection cannot be due before the device was added."
    assert not Asset.objects.filter(tag="CE-30002").exists()


def test_assign_the_inspection_to_me_takes_it_when_credentialed(client, setup):
    client.force_login(setup["dana"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], take_inspection="on"), **HX)
    wo = inspections.open_inspection(added())
    assert wo.assigned_to == setup["dana_tech"] and toast_of(r) == f"CE-30001 added; {wo.number} opened for its incoming inspection and assigned to you"
    assert WorkOrderStatusHistory.objects.filter(work_order=wo, note="Assigned to Dana Whitfield").exists()  # through assign(): no override


def test_take_refused_still_adds_the_device_and_says_why(client, setup):
    client.force_login(setup["tom"])  # not credentialed for the ventilator
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], take_inspection="on"), **HX)
    a = added()
    wo = inspections.open_inspection(a)
    assert a.awaiting_inspection and wo.assigned_to is None and r["HX-Retarget"] == "#drawer"
    assert toast_of(r) == (f"CE-30001 added; {wo.number} opened for its incoming inspection. You are not credentialed for the "
                           f"{a.device_model} (CE-30001), so a CE manager assigns {wo.number}.")


def test_a_manager_picks_the_inspector_a_technician_or_vendor_service(client, setup):
    client.force_login(setup["kim"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], inspector=str(setup["tom_tech"].pk)), **HX)
    wo = inspections.open_inspection(added())
    assert wo.assigned_to == setup["tom_tech"] and toast_of(r).endswith("opened for its incoming inspection and assigned to Tom Okafor")
    assert WorkOrderStatusHistory.objects.filter(work_order=wo, note="Assigned to Tom Okafor (override: not credentialed for this device)").exists()
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], tag="CE-30002", inspector="vendor"), **HX)
    wo = inspections.open_inspection(added("CE-30002"))
    assert (wo.vendor_service, wo.vendor_name, wo.assigned_to) == (True, "Hamilton Medical field service", None)
    assert toast_of(r).endswith("and sent to vendor service (Hamilton Medical field service)")
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], tag="CE-30003", inspector="nobody"), **HX)
    assert field_error(r.content.decode(), "inspector") == "Choose an inspector from the list." and not Asset.objects.filter(tag="CE-30003").exists()


def test_the_assignment_fields_of_someone_else_change_nothing(client, setup):
    """Posted by a user the form does not offer them to: a technician's inspector, a manager's take box."""
    client.force_login(setup["dana"])
    client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], take_inspection="", inspector=str(setup["tom_tech"].pk)), **HX)
    assert inspections.open_inspection(added()).assigned_to is None
    client.force_login(setup["kim"])
    client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], tag="CE-30002", take_inspection="on"), **HX)
    assert inspections.open_inspection(added("CE-30002")).assigned_to is None


def test_a_waiting_device_ignores_the_existing_equipment_fields(client, setup, today):
    """Hidden on screen while "waiting" is chosen: whatever the browser kept in them is dropped, never refused or saved."""
    client.force_login(setup["dana"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], take_inspection="", status=S.RETIRED, last_pm_on="soon",
                                      next_pm_on=(today + timedelta(days=9)).isoformat()), **HX)
    a = added()
    assert r["HX-Retarget"] == "#drawer" and (a.status, a.last_pm_on, a.next_pm_on, a.awaiting_inspection) == (S.OUT_OF_SERVICE, None, None, True)


def test_existing_equipment_ignores_the_inspection_fields(client, setup, today):
    client.force_login(setup["kim"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], intake="existing", installed_on="2021-05-01", inspection_due="x",
                                      inspector="nobody", status=S.OUT_OF_SERVICE), **HX)
    a = added()
    assert toast_of(r) == "CE-30001 added" and "wo-changed" not in triggers(r)
    assert (a.added_as, a.status, a.awaiting_inspection, a.work_orders.count()) == (AddedAs.EXISTING, S.OUT_OF_SERVICE, False, 0)
    assert a.next_pm_on is not None


# --- Add device: inspect it now -----------------------------------------------------------------------------------------------------

def test_inspect_it_now_takes_the_inspection_and_opens_its_mark_completed(client, setup, today):
    client.force_login(setup["dana"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], intake="inspect", inspection_due="2000-01-01", take_inspection=""), **HX)
    a = added()
    wo = inspections.open_inspection(a)
    assert a.awaiting_inspection and a.status == S.OUT_OF_SERVICE and wo.assigned_to == setup["dana_tech"]
    assert wo.due_on == today + timedelta(days=inspections.INSPECTION_DUE_DAYS)  # the hidden due date was dropped
    assert r.status_code == 200 and r.content == b"" and "HX-Retarget" not in r and "HX-Trigger-After-Settle" not in r  # the modal stays open
    assert json.loads(r["HX-Location"]) == {"path": f"/work-orders/{wo.number}/complete/", "target": "#modal-card", "swap": "innerHTML",
                                            "push": "false"}
    t = triggers(r)
    assert t["toast"]["value"] == f"CE-30001 added; record its incoming inspection, {wo.number}" and "devices-changed" in t and "wo-changed" in t
    # what htmx then fetches: the inspection's Mark completed, ready (open, assigned, completed in one step)
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    assert f"<h2>Complete {wo.number}</h2>" in body and 'id="wc-form"' in body


def test_a_manager_inspecting_it_now_assigns_it_to_her_own_profile(client, setup):
    client.force_login(setup["kim"])
    r = client.post(NEW_URL, new_post(setup["pump_model"], setup["dept"], intake="inspect"), **HX)
    wo = inspections.open_inspection(added())
    assert wo.assigned_to == setup["kim_tech"] and "HX-Location" in r
    assert WorkOrderStatusHistory.objects.filter(work_order=wo, note="Assigned to Kim Alvarez (override: not credentialed for this device)").exists()


def test_inspect_it_now_refused_by_take_answers_with_the_drawer_and_why(client, setup):
    client.force_login(setup["tom"])
    r = client.post(NEW_URL, new_post(setup["vent_model"], setup["dept"], intake="inspect"), **HX)
    wo = inspections.open_inspection(added())
    assert "HX-Location" not in r and r["HX-Retarget"] == "#drawer" and wo.assigned_to is None
    assert toast_of(r).startswith(f"CE-30001 added; {wo.number} opened for its incoming inspection. You are not credentialed for")


# --- Edit details -------------------------------------------------------------------------------------------------------------------

def test_edit_details_of_a_waiting_device_shows_its_next_pm_as_words_and_saves(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", department=setup["dept"], today=today)
    client.force_login(setup["dana"])
    body = client.get("/equipment/CE-30001/edit/", **HX).content.decode()
    assert 'name="next_pm_on"' not in body and 'value="After its incoming inspection" readonly' in body
    assert "Its PM schedule starts on the day its incoming inspection passes." in body
    post = {"serial": "SWAP-2", "device_model": str(a.device_model_id), "department": str(a.department_id), "room": "B12", "installed_on": "",
            "acquisition_cost": "38000", "warranty_end": "", "condition": "4", "notes": "", "next_pm_on": (today + timedelta(days=30)).isoformat()}
    r = client.post("/equipment/CE-30001/edit/", post, **HX)
    a.refresh_from_db()
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == "CE-30001 updated"
    assert (a.serial, a.room, a.next_pm_on, a.awaiting_inspection) == ("SWAP-2", "B12", None, True)  # the posted date never reaches the service


# --- the drawer ---------------------------------------------------------------------------------------------------------------------

def drawer(client, tag, tab=""):
    return client.get(f"/equipment/{tag}/" + (f"?tab={tab}" if tab else ""), **HX).content.decode()


def link(number) -> str:
    return f'<a class="link" href="/work-orders/{number}/" hx-get="/work-orders/{number}/" hx-target="#drawer">{number}</a>'


def test_the_drawer_of_a_waiting_device(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", department=setup["dept"], today=today)
    wo = inspections.open_inspection(a)
    client.force_login(setup["dana"])
    body = drawer(client, a.tag)
    assert '<span class="chip info">Awaiting inspection</span>' in body and '<span class="stk insp"' in body and ">After inspection</span>" in body
    assert f"<p>Waiting for its incoming inspection: {link(wo.number)}, due {day(wo.due_on)}.</p>" in body
    assert "<dt>Next PM</dt><dd>After its incoming inspection</dd>" in body
    assert "Open incoming inspection" not in body and "Put in use before inspection" not in body  # one is open; Equipment Edit only
    buttons = re.findall(r'hx-vals=\'\{"to": "(\w+)"\}\'[^>]*>([^<]+)</button>', body)
    assert dict((label, to) for to, label in buttons) == {"Mark missing": S.MISSING}  # never Return to service while it waits


def test_the_drawer_after_a_fail_and_with_none_open(client, setup, today):
    a, failed, again = failed_device(setup["vent_model"], "CE-30001", technician=setup["dana_tech"], today=today)
    client.force_login(setup["dana"])
    body = drawer(client, a.tag)
    assert 'class="note ib-note warn"' in body
    assert (f"<p>Failed its incoming inspection on {day(today)} ({link(failed.number)}); re-inspection {link(again.number)} open, due "
            f"{day(again.due_on)}.</p>") in body
    change_status(again, WoStatus.CANCELLED)
    body = drawer(client, a.tag)
    assert "no re-inspection is open.</p>" in body
    offer = ('<button class="link" type="button" hx-get="/work-orders/new/?asset=CE-30001&amp;type=inspection" hx-target="#modal-card">'
             "Open incoming inspection</button>")
    assert offer in body
    # New work order opens with the device and the type filled in
    form = client.get("/work-orders/new/?asset=CE-30001&type=inspection", **HX).content.decode()
    assert '<option value="inspection" selected>' in form and 'value="CE-30001"' in form


def test_open_incoming_inspection_needs_work_orders_edit(client, setup, make_user, today):
    a = waiting_device(setup["vent_model"], "CE-30001", department=setup["dept"], today=today)
    change_status(inspections.open_inspection(a), WoStatus.CANCELLED)
    client.force_login(make_user("analyst"))  # Work orders View: the words, not the button
    body = drawer(client, a.tag)
    assert "Waiting for its incoming inspection; none is open." in body and "Open incoming inspection" not in body


def test_the_drawer_of_a_device_in_use_before_its_inspection(client, setup, today):
    a = in_use_device(setup["vent_model"], "CE-30001", by=setup["director"], today=today)
    wo = inspections.open_inspection(a)
    client.force_login(setup["dana"])
    body = drawer(client, a.tag)
    assert '<span class="chip ok">In service</span>' in body and ">After inspection</span>" in body and 'class="note ib-note warn"' in body
    assert f"<p>In use before its incoming inspection: Emergency clinical need (since {day(today)}).</p>" in body
    assert f"<p>Waiting for its incoming inspection: {link(wo.number)}, due {day(today + timedelta(days=1))}.</p>" in body


def test_a_retired_waiting_device_says_so_and_offers_nothing(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    eq.set_status(a, S.RETIRED)
    client.force_login(setup["director"])
    body = drawer(client, a.tag)
    assert asset_tabs.RETIRED_WAITING in body and "Open incoming inspection" not in body and "Put in use before inspection" not in body
    assert "<dt>Next PM</dt><dd>—</dd>" in body and '<span class="chip neutral">Retired</span>' in body


def test_a_device_not_waiting_has_no_banner(client, setup, vent, today):
    a, _wo = passed_device(setup["vent_model"], "CE-30001", technician=setup["dana_tech"], today=today)
    client.force_login(setup["dana"])
    for tag in (vent.tag, a.tag):
        body = drawer(client, tag)
        assert "ib-note" not in body and "Awaiting inspection" not in body and "After inspection" not in body
    assert asset_tabs.incoming_banner(vent, setup["dana"]) is None


def test_a_scoped_user_sees_the_banner_masked_and_no_actions(client, setup, make_user, today):
    a, failed, again = failed_device(setup["vent_model"], "CE-30001", technician=setup["dana_tech"], today=today)
    vendor = make_user("vendor")
    vendor.company = "Hamilton Service"
    vendor.save()
    assign(again, vendor_name="Hamilton Service")  # the re-inspection is the vendor's: the device is in their share
    client.force_login(vendor)
    body = drawer(client, a.tag)
    assert f"<p>Failed its incoming inspection on {day(today)}; re-inspection {link(again.number)} open" in body
    assert failed.number not in body
    assert "Open incoming inspection" not in body and "Put in use before inspection" not in body and "/status/" not in body


# --- put in use before its inspection -----------------------------------------------------------------------------------------------

def test_put_in_use_before_inspection_is_equipment_approves(client, setup, make_user, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    url = "/equipment/CE-30001/use-before-inspection/"
    for user in (setup["dana"], setup["kim"]):  # Equipment Edit
        client.force_login(user)
        assert "Put in use before inspection" not in drawer(client, a.tag)
        assert client.get(url, **HX).status_code == 403
        assert client.post(url, {"reason": UseBeforeInspection.EMERGENCY}, **HX).status_code == 403
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE
    client.force_login(setup["director"])
    assert f'hx-get="{url}" hx-target="#modal-card"' in drawer(client, a.tag)


def test_putting_it_in_use_before_its_inspection(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    wo = inspections.open_inspection(a)
    url = "/equipment/CE-30001/use-before-inspection/"
    client.force_login(setup["director"])
    body = client.get(url, **HX).content.decode()
    assert "<h2>Put CE-30001 in use</h2>" in body and f"({wo.number})" in body and f'hx-post="{url}"' in body
    for value, label in UseBeforeInspection.choices:
        assert f'value="{value}"' in body and label in body
    r = client.post(url, {}, **HX)
    assert "Choose why the device goes into use before its incoming inspection." in r.content.decode() and "HX-Retarget" not in r
    r = client.post(url, {"reason": "patient asked"}, **HX)
    assert "Choose one of the reasons listed." in r.content.decode()
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE
    r = client.post(url, {"reason": UseBeforeInspection.LOANER_RENTAL}, **HX)
    a.refresh_from_db(), wo.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.IN_SERVICE, True, None)
    assert (wo.priority, wo.due_on) == (Priority.HIGH, today + timedelta(days=1))
    assert r["HX-Retarget"] == "#drawer" and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    t = triggers(r)
    assert t["toast"]["value"] == (f"CE-30001 in use before its incoming inspection (Loaner or rental needed now); {wo.number} is high priority, "
                                   f"due {day(today + timedelta(days=1))}")
    assert "devices-changed" in t and "wo-changed" in t
    assert "In use before its incoming inspection: Loaner or rental needed now" in r.content.decode()


def test_a_device_no_longer_waiting_gets_the_modal_saying_why(client, setup, vent, today):
    client.force_login(setup["director"])
    url = f"/equipment/{vent.tag}/use-before-inspection/"
    for r in (client.get(url, **HX), client.post(url, {"reason": UseBeforeInspection.EMERGENCY}, **HX)):
        body = r.content.decode()
        assert r.status_code == 200 and "CE-10001 is not waiting for an incoming inspection." in body and 'name="reason"' not in body
    used = in_use_device(setup["vent_model"], "CE-30001", today=today)
    body = client.get("/equipment/CE-30001/use-before-inspection/", **HX).content.decode()
    assert "CE-30001 is in service; only a device out of service or missing" in body
    vent.refresh_from_db(), used.refresh_from_db()
    assert vent.status == used.status == S.IN_SERVICE


def test_another_facilitys_device_is_a_404(client, setup, other_tenant):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        theirs = waiting_device(dm, "THEIRS-1")
    client.force_login(setup["director"])
    url = f"/equipment/{theirs.tag}/use-before-inspection/"
    assert client.get(url, **HX).status_code == 404
    assert client.post(url, {"reason": UseBeforeInspection.EMERGENCY}, **HX).status_code == 404
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.status == S.OUT_OF_SERVICE


# --- status buttons and toasts ------------------------------------------------------------------------------------------------------

def test_retiring_and_reinstating_a_waiting_device_from_the_drawer(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    wo = inspections.open_inspection(a)
    client.force_login(setup["director"])
    r = client.post("/equipment/CE-30001/status/", {"to": S.RETIRED}, **HX)
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED and toast_of(r) == "CE-30001 retired; 1 incoming inspection cancelled"
    assert re.search(r'hx-vals=\'\{"to": "out_of_service"\}\'[^>]*>Reinstate</button>', r.content.decode())
    r = client.post("/equipment/CE-30001/status/", {"to": S.OUT_OF_SERVICE}, **HX)
    a.refresh_from_db()
    again = inspections.open_inspection(a)
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, True, None) and again is not None
    assert toast_of(r) == "CE-30001 reinstated; it stays out of service until its incoming inspection passes" and "wo-changed" in triggers(r)
    assert f"Waiting for its incoming inspection: {link(again.number)}" in r.content.decode()


def test_missing_and_found_on_a_waiting_device(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    client.force_login(setup["dana"])
    client.post("/equipment/CE-30001/status/", {"to": S.MISSING}, **HX)
    body = drawer(client, a.tag)
    assert re.search(r'hx-vals=\'\{"to": "out_of_service"\}\'[^>]*>Found</button>', body) and ">Return to service<" not in body
    r = client.post("/equipment/CE-30001/status/", {"to": S.IN_SERVICE}, **HX)  # never by hand while it waits
    assert toast_of(r).startswith("CE-30001 is waiting for its incoming inspection (") and "devices-changed" not in triggers(r)
    r = client.post("/equipment/CE-30001/status/", {"to": S.OUT_OF_SERVICE}, **HX)
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE and toast_of(r) == "CE-30001 found; it stays out of service until its incoming inspection passes"


# --- the Equipment table, the CSV, and the PM tab -----------------------------------------------------------------------------------

def test_the_equipment_table_and_csv_word_the_status_as_every_screen(client, setup, vent, today):
    waiting_device(setup["vent_model"], "CE-30001", today=today)
    in_use_device(setup["vent_model"], "CE-30002", today=today)
    client.force_login(setup["dana"])
    page = client.get("/equipment/").content.decode()
    row = page.split('<a class="tag" href="/equipment/CE-30001/">', 1)[1].split("</tr>", 1)[0]
    assert '<span class="chip info">Awaiting inspection</span>' in row and ">After inspection</span>" in row
    row = page.split('<a class="tag" href="/equipment/CE-30002/">', 1)[1].split("</tr>", 1)[0]
    assert '<span class="chip ok">In service</span>' in row and ">After inspection</span>" in row
    rows = csv_rows(client.get("/export/equipment.csv"))
    status = {r[0]: dict(zip(rows[0], r))["Status"] for r in rows[1:]}
    assert status == {"CE-10001": "In service", "CE-30001": "Awaiting inspection", "CE-30002": "In service"}


def test_the_pm_tab_of_a_waiting_device_has_no_upcoming_pms(client, setup, today):
    a = waiting_device(setup["vent_model"], "CE-30001", today=today)
    up = asset_tabs.pm_tab(a)["pm"]["upcoming"]
    assert up["rows"] == [] and up["unscheduled"] == "After its incoming inspection: its PM schedule starts on the day the inspection passes."
    client.force_login(setup["dana"])
    body = drawer(client, a.tag, "pm")
    assert "After its incoming inspection: its PM schedule starts on the day the inspection passes." in body
    wo = inspections.open_inspection(a)
    assert "<span>0 PM work orders · 1 incoming inspection</span>" in body
    assert link(wo.number) + '<small class="muted pm-insp">Incoming inspection</small>' in body


def test_the_pm_tab_lists_the_incoming_inspection_as_the_first_maintenance_record(client, setup, today):
    a, failed, again = failed_device(setup["vent_model"], "CE-30001", technician=setup["dana_tech"], today=today - timedelta(days=20))
    complete_work_order(again, inspection_result="passed", results=INCOMING_ALL_PASS, today=today - timedelta(days=10))
    a.refresh_from_db()
    assert not a.awaiting_inspection and a.status == S.IN_SERVICE
    rows = asset_tabs.pm_tab(a)["pm"]["history"]
    assert [(h["wo"].number, h["inspection"], h["result"]) for h in rows] == [
        (again.number, True, "Passed"), (failed.number, True, "Failed, re-inspection opened")]
    assert rows[1]["repair"] == again and rows[1]["css"] == "down"
    client.force_login(setup["dana"])
    body = drawer(client, a.tag, "pm")
    assert "<span>0 PM work orders · 2 incoming inspections</span>" in body
    assert ('<td class="down" style="white-space:normal;min-width:140px">Failed, re-inspection opened · '
            f'<a class="link" href="/work-orders/{again.number}/" hx-get="/work-orders/{again.number}/" hx-target="#drawer" style="white-space:nowrap">'
            f'{again.number}</a></td>') in body
    assert '<td style="white-space:normal;min-width:140px">Passed</td>' in body
    assert "Next due" in body  # its PM schedule started at the pass
