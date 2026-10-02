"""Device management in the web UI (slice 12): Add device, Edit details, and the device drawer's status buttons. Permissions are
checked server-side (a hidden button is not access control), changes go through apps.equipment.services, another facility's device
is a 404, and the modal and drawer follow the shell's HTMX conventions."""
import inspect
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import create_default_roles
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.web.forms_equipment import LAYOUT, MODEL_FIELDS, EditDeviceForm, NewDeviceForm
from apps.web.views_equipment import STATUS_TOASTS, status_toast
from apps.workorders.models import WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
NEW_URL = "/equipment/new/"


def active_tab(tab, tag="CE-10001"):
    return f'class="tab active" role="tab" aria-selected="true" hx-get="/equipment/{tag}/?tab={tab}"'


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's department, model, and device (tag THEIRS-1)."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, next_pm_on=TODAY + timedelta(days=30))
    return {"department": d, "model": dm, "asset": asset}


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def new_post(dm, dept, **over) -> dict:
    data = {"tag": "CE-20001", "serial": "SN-77", "device_model": str(dm.pk) if dm else "new", "department": str(dept.pk) if dept else "new",
            "room": "412", "installed_on": "", "acquisition_cost": "", "warranty_end": "", "condition": "4", "last_pm_on": "", "next_pm_on": "",
            "status": AssetStatus.IN_SERVICE, "notes": "Wall mount, bay 3", "risk_class": RiskClass.MEDIUM, "oem_pm_interval_months": "12",
            "expected_life_years": "8", "list_cost": "", "manufacturer": "", "model": "", "description": "", "category": "", "new_department": ""}
    data.update(over)
    return data


def new_model(**over) -> dict:
    return {"device_model": "new", "manufacturer": "Mindray", "model": "BeneVision N12", "description": "Patient monitor", "category": "Monitors",
            "risk_class": RiskClass.HIGH, "oem_pm_interval_months": "12", "expected_life_years": "7", "list_cost": "9500", **over}


def edit_post(asset, **over) -> dict:
    data = {"serial": asset.serial, "device_model": str(asset.device_model_id), "department": str(asset.department_id), "room": asset.room,
            "installed_on": asset.installed_on.isoformat() if asset.installed_on else "", "acquisition_cost": str(asset.acquisition_cost),
            "warranty_end": asset.warranty_end.isoformat() if asset.warranty_end else "", "condition": str(asset.condition),
            "next_pm_on": asset.next_pm_on.isoformat() if asset.next_pm_on else "", "notes": asset.notes}
    data.update(over)
    return data


def field_error(body: str, field: str) -> str:
    """The error text shown under one field of the device form ('' when there is none)."""
    marker = f'id="dv-{field}_error">'
    if marker not in body:
        return ""
    return body.split(marker, 1)[1].split("</span>", 1)[0]


def status_url(asset, tab=""):
    return f"/equipment/{asset.tag}/status/" + (f"?tab={tab}" if tab else "")


# --- Add device: access ---------------------------------------------------------------------------------------------

def test_add_device_modal_renders_for_equipment_edit(client, signed_in, vent_model, pump_model, dept):
    signed_in("technician")  # Equipment Edit
    r = client.get(NEW_URL, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>Add device</h2>" in body and f'hx-post="{NEW_URL}"' in body and 'hx-target="#modal-card"' in body
    assert "Hamilton Medical Hamilton-G5 · ICU ventilator" in body and "BD Alaris 8015 PCU · Infusion pump" in body
    assert '<option value="new">Add a new model</option>' in body and '<option value="new">Add a new department</option>' in body
    assert '<option value="Ventilators">' in body and '<option value="Infusion pumps">' in body  # category suggestions
    assert "Out of service: waiting for incoming inspection" in body and "No patient information." in body
    assert "Blank uses the model&#x27;s list cost." in body and "Blank lets the schedule work it out" in body
    assert '<option value="3" selected>3 · Good</option>' in body  # condition defaults to 3
    # the hint and the error are tied to their input for screen readers
    assert 'aria-describedby="dv-next_pm_on_helptext"' in body and 'id="dv-next_pm_on_helptext"' in body


def test_add_device_needs_equipment_edit(client, signed_in, vent_model, dept):
    signed_in("requester")  # Equipment View
    assert client.get(NEW_URL, **HX).status_code == 403
    assert client.post(NEW_URL, new_post(vent_model, dept), **HX).status_code == 403
    assert not Asset.objects.exists()
    page = client.get("/equipment/").content.decode()
    assert f'hx-get="{NEW_URL}"' not in page and "Add device" not in page


def test_add_device_button_shows_for_equipment_edit(client, signed_in):
    signed_in("technician")
    page = client.get("/equipment/").content.decode()
    assert f'hx-get="{NEW_URL}" hx-target="#modal-card"' in page and "Add device" in page


def test_equipment_table_refreshes_when_devices_change(client, signed_in, vent):
    signed_in("technician")
    page = client.get("/equipment/").content.decode()
    table = page.split('id="eq-table"', 1)[1].split(">", 1)[0]
    assert "devices-changed from:body" in table and 'hx-disinherit="hx-swap"' in table


# --- Add device: creating ---------------------------------------------------------------------------------------------

def test_create_with_an_existing_model_and_department(client, signed_in, pump_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(pump_model, dept, installed_on=(TODAY - timedelta(days=30)).isoformat()), **HX)
    a = Asset.objects.select_related("device_model", "department").get(tag="CE-20001")
    assert (a.device_model, a.department, a.serial, a.room, a.condition, a.notes) == (pump_model, dept, "SN-77", "412", 4, "Wall mount, bay 3")
    assert a.status == AssetStatus.IN_SERVICE and a.acquisition_cost == pump_model.list_cost  # blank cost: the model's list cost
    # blank next PM: one interval (AEM 18 months for this model) after install
    assert a.next_pm_on == add_months(TODAY - timedelta(days=30), 18)
    assert a.history.first().history_change_reason == "Added"
    # the response opens the new device's drawer, refreshes the table, toasts, and closes the modal after the swap settles
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    assert "<h2>CE-20001 · Infusion pump</h2>" in r.content.decode()
    t = triggers(r)
    assert t["toast"] == {"value": "CE-20001 added"} and "devices-changed" in t
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_create_with_a_new_model_and_a_new_department_in_one_go(client, signed_in, vent_model, dept):
    signed_in("technician")
    installed = TODAY - timedelta(days=400)
    r = client.post(NEW_URL, new_post(None, None, **new_model(), new_department="  Step-down   unit ", installed_on=installed.isoformat(),
                                      acquisition_cost="9100", status=AssetStatus.OUT_OF_SERVICE), **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    dm = DeviceModel.objects.get(model="BeneVision N12")
    assert (dm.manufacturer, dm.description, dm.category, dm.risk_class, dm.oem_pm_interval_months, dm.expected_life_years, dm.list_cost) == (
        "Mindray", "Patient monitor", "Monitors", RiskClass.HIGH, 12, 7, Decimal("9500"))
    new_dept = Department.objects.get(name="Step-down unit")
    a = Asset.objects.get(tag="CE-20001")
    assert (a.device_model, a.department, a.acquisition_cost, a.status) == (dm, new_dept, Decimal("9100"), AssetStatus.OUT_OF_SERVICE)
    assert a.next_pm_on == TODAY  # a year's interval since install has passed and no PM is on record: due today


def test_a_new_department_named_like_an_existing_one_reuses_it(client, signed_in, pump_model, dept):
    signed_in("technician")
    client.post(NEW_URL, new_post(pump_model, None, new_department="icu"), **HX)
    assert Asset.objects.get(tag="CE-20001").department == dept and Department.objects.count() == 1


def test_last_and_next_pm_as_typed(client, signed_in, pump_model, dept):
    signed_in("technician")
    last = TODAY - timedelta(days=100)
    client.post(NEW_URL, new_post(pump_model, dept, last_pm_on=last.isoformat()), **HX)
    assert Asset.objects.get(tag="CE-20001").next_pm_on == add_months(last, 18)
    nxt = TODAY + timedelta(days=12)
    client.post(NEW_URL, new_post(pump_model, dept, tag="CE-20002", last_pm_on=last.isoformat(), next_pm_on=nxt.isoformat()), **HX)
    assert Asset.objects.get(tag="CE-20002").next_pm_on == nxt


@pytest.mark.parametrize("over, field, message", [
    ({"tag": "ce-10001"}, "tag", "ce-10001 is already on another device."),  # vent is CE-10001: unique in any letter case
    ({"tag": "CE 1"}, "tag", "Asset tags cannot contain spaces or slashes."),
    ({"tag": "CE/1"}, "tag", "Asset tags cannot contain spaces or slashes."),
    ({"tag": "New"}, "tag", "&quot;New&quot; cannot be used as an asset tag."),  # /equipment/new/ is this form
    ({"tag": ""}, "tag", "Enter the asset tag from the CE sticker."),
    ({"installed_on": (TODAY + timedelta(days=1)).isoformat()}, "installed_on", "The install date cannot be in the future."),
    ({"installed_on": "2024-05-01", "warranty_end": "2024-04-30"}, "warranty_end", "The warranty cannot end before the device was installed."),
    ({"last_pm_on": (TODAY + timedelta(days=3)).isoformat()}, "last_pm_on", "The last PM cannot be in the future."),
    ({"acquisition_cost": "-5"}, "acquisition_cost", "The acquisition cost cannot be negative."),
    ({"status": AssetStatus.RETIRED}, "status", "Select a valid choice."),
    ({"device_model": ""}, "device_model", "Choose a model."),
    ({"department": ""}, "department", "Choose a department."),
])
def test_field_errors_land_on_their_fields(client, signed_in, vent, pump_model, dept, over, field, message):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(pump_model, dept, **over), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "HX-Trigger" not in r and "<h2>Add device</h2>" in body
    assert message in field_error(body, field), body
    assert 'aria-invalid="true"' in body
    assert Asset.objects.count() == 1  # only the vent


@pytest.mark.parametrize("over, field, message", [
    ({"manufacturer": ""}, "manufacturer", "This is required."),
    ({"category": "  "}, "category", "This is required."),
    ({"manufacturer": "bd", "model": "alaris 8015 pcu"}, "model", "is already in the catalog; choose it from the list."),
    ({"oem_pm_interval_months": ""}, "oem_pm_interval_months", "The PM interval is 1 to 120 months."),
    ({"expected_life_years": "70"}, "expected_life_years", "Expected life is 1 to 50 years."),
    ({"risk_class": ""}, "risk_class", "Choose a risk class."),
])
def test_new_model_errors_land_on_its_fields(client, signed_in, pump_model, dept, over, field, message):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(None, dept, **new_model(**over)), **HX)
    assert message in field_error(r.content.decode(), field)
    assert DeviceModel.objects.count() == 1 and not Asset.objects.exists()


def test_a_new_department_needs_a_name(client, signed_in, pump_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(pump_model, None, new_department="   "), **HX)
    assert field_error(r.content.decode(), "new_department") == "Enter the department&#x27;s name."
    assert not Asset.objects.exists()


def test_a_failing_device_leaves_no_new_model_or_department_behind(client, signed_in, vent, dept):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(None, None, **new_model(), new_department="Cath lab", tag="CE 20001"), **HX)
    body = r.content.decode()
    assert "Asset tags cannot contain spaces" in field_error(body, "tag")
    assert DeviceModel.objects.count() == 1 and Department.objects.count() == 1 and Asset.objects.count() == 1
    # the form comes back as typed, still on "Add a new model", so fixing the tag is all it takes
    assert '<option value="new" selected>Add a new model</option>' in body and 'value="Mindray"' in body and 'value="Cath lab"' in body
    r = client.post(NEW_URL, new_post(None, None, **new_model(), new_department="Cath lab"), **HX)
    assert r["HX-Retarget"] == "#drawer" and Asset.objects.get(tag="CE-20001").device_model.manufacturer == "Mindray"


def test_hidden_fields_never_block_an_existing_model_or_department(client, signed_in, pump_model, dept):
    """The new model's fields and the department name are ignored, whatever they hold, unless "Add a new ..." is chosen."""
    signed_in("technician")
    r = client.post(NEW_URL, new_post(pump_model, dept, list_cost="1.234", oem_pm_interval_months="x", manufacturer="M" * 300, new_department="D" * 300),
                    **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert DeviceModel.objects.count() == 1 and Department.objects.count() == 1 and Asset.objects.get(tag="CE-20001").device_model == pump_model


def test_another_facilitys_model_or_department_cannot_be_chosen(client, signed_in, pump_model, dept, theirs):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(theirs["model"], dept), **HX)
    assert field_error(r.content.decode(), "device_model") == "Choose a model from the list."
    r = client.post(NEW_URL, new_post(pump_model, theirs["department"]), **HX)
    assert field_error(r.content.decode(), "department") == "Choose a department from the list."
    assert not Asset.objects.exists()
    body = client.get(NEW_URL, **HX).content.decode()
    assert "Their pump" not in body and "Their ICU" not in body


# --- Edit details --------------------------------------------------------------------------------------------------------

def test_edit_modal_is_prefilled(client, signed_in, vent, pump_model):
    Asset.objects.filter(pk=vent.pk).update(serial="HM-123", room="7", notes="Spare circuit in drawer", condition=4,
                                            warranty_end=TODAY + timedelta(days=200))
    signed_in("technician")
    r = client.get(f"/equipment/{vent.tag}/edit/", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>Edit CE-10001</h2>" in body and 'hx-post="/equipment/CE-10001/edit/"' in body
    for value in ('value="HM-123"', 'value="7"', f'value="{vent.installed_on.isoformat()}"', 'value="38000.00"',
                  f'value="{(TODAY + timedelta(days=200)).isoformat()}"', f'value="{vent.next_pm_on.isoformat()}"', "Spare circuit in drawer"):
        assert value in body, value
    assert f'<option value="{vent.device_model_id}" selected>Hamilton Medical Hamilton-G5 · ICU ventilator</option>' in body
    assert f'<option value="{vent.department_id}" selected>ICU</option>' in body and '<option value="4" selected>' in body
    # the tag is shown, never posted, with the reason; no "Add a new ..." choices here, no status or last PM
    assert 'value="CE-10001" readonly' in body and "Cannot be changed: it is on the label and in links." in body
    assert 'name="tag"' not in body and 'value="new"' not in body and 'name="status"' not in body and 'name="last_pm_on"' not in body
    assert "Stays as it is when the model changes." in body


def test_edit_saves_and_reopens_the_drawer_on_overview(client, signed_in, vent, pump_model):
    signed_in("technician")
    nxt = TODAY + timedelta(days=60)
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, serial=" HM-9 ", room="12B", device_model=str(pump_model.pk), condition="2",
                                                          acquisition_cost="36500", next_pm_on=nxt.isoformat(), notes="Moved to bay 2"), **HX)
    vent.refresh_from_db()
    assert (vent.serial, vent.room, vent.device_model, vent.condition, vent.acquisition_cost, vent.next_pm_on, vent.notes) == (
        "HM-9", "12B", pump_model, 2, Decimal("36500"), nxt, "Moved to bay 2")
    assert vent.history.first().history_change_reason == "Edited"
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    body = r.content.decode()
    assert "<h2>CE-10001 · Infusion pump</h2>" in body and active_tab("overview") in body
    t = triggers(r)
    assert t["toast"] == {"value": "CE-10001 updated"} and "devices-changed" in t
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")


def test_changing_the_model_keeps_the_next_pm_unless_it_is_changed(client, signed_in, vent, pump_model):
    signed_in("technician")
    before = vent.next_pm_on
    client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, device_model=str(pump_model.pk)), **HX)
    vent.refresh_from_db()
    assert vent.device_model == pump_model and vent.next_pm_on == before


def test_edit_cannot_change_the_tag_status_or_contract_even_if_posted(client, signed_in, vent):
    signed_in("director")
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, tag="CE-99999", status=AssetStatus.RETIRED, contract="x", support_type="third_party"),
                    **HX)
    assert r["HX-Retarget"] == "#drawer"
    vent.refresh_from_db()
    assert (vent.tag, vent.status, vent.contract_id) == ("CE-10001", AssetStatus.IN_SERVICE, None)
    assert not Asset.objects.filter(tag="CE-99999").exists()


@pytest.mark.parametrize("over, field, message", [
    ({"next_pm_on": ""}, "next_pm_on", "A device in use needs a next PM date."),
    ({"warranty_end": "2020-01-01", "installed_on": "2021-01-01"}, "warranty_end", "The warranty cannot end before the device was installed."),
    ({"installed_on": (TODAY + timedelta(days=2)).isoformat()}, "installed_on", "The install date cannot be in the future."),
    ({"acquisition_cost": ""}, "acquisition_cost", "This field is required."),
    ({"acquisition_cost": "-1"}, "acquisition_cost", "The acquisition cost cannot be negative."),
    ({"condition": "9"}, "condition", "Condition is 1 (poor) to 5 (excellent)."),
    ({"device_model": "new"}, "device_model", "Choose a model from the list."),
])
def test_edit_errors_land_on_their_fields(client, signed_in, vent, over, field, message):
    signed_in("technician")
    before = (vent.next_pm_on, vent.warranty_end, vent.installed_on, vent.acquisition_cost, vent.condition)
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, **over), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "<h2>Edit CE-10001</h2>" in body
    assert message in field_error(body, field), body
    vent.refresh_from_db()
    assert (vent.next_pm_on, vent.warranty_end, vent.installed_on, vent.acquisition_cost, vent.condition) == before


def test_a_retired_device_can_be_saved_without_a_next_pm(client, signed_in, vent):
    eq.set_status(vent, AssetStatus.RETIRED)
    signed_in("technician")
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, notes="Kept for parts"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    vent.refresh_from_db()
    assert vent.notes == "Kept for parts" and vent.next_pm_on is None


def test_a_device_with_an_older_pm_than_its_install_date_stays_editable(client, signed_in, vent):
    """Imported and demo devices can have a last PM before their install date. Editing anything else still saves; moving the
    install date later than that PM is refused on the install date (the form has no last PM field), not as a crash."""
    Asset.objects.filter(pk=vent.pk).update(last_pm_on=vent.installed_on - timedelta(days=30))
    vent.refresh_from_db()
    signed_in("technician")
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, room="3"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    later = vent.installed_on + timedelta(days=10)
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, room="3", installed_on=later.isoformat()), **HX)
    assert r.status_code == 200 and field_error(r.content.decode(), "installed_on").startswith("The install date cannot be after the last PM on record")


def test_edit_needs_equipment_edit(client, signed_in, vent):
    requester = signed_in("requester")
    requester.department = "ICU"  # slice 16: a requester sees their own unit's devices
    requester.save()
    assert client.get(f"/equipment/{vent.tag}/edit/", **HX).status_code == 403
    assert client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, room="99"), **HX).status_code == 403
    vent.refresh_from_db()
    assert vent.room == ""
    r = client.get(f"/equipment/{vent.tag}/", **HX)
    drawer = r.content.decode()
    assert r.status_code == 200 and "/edit/" not in drawer and "Edit details" not in drawer


def test_the_drawer_offers_edit_details_to_equipment_edit(client, signed_in, vent):
    signed_in("technician")
    drawer = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert 'hx-get="/equipment/CE-10001/edit/" hx-target="#modal-card">Edit details</button>' in drawer


def test_unknown_tag_is_a_404(client, signed_in, vent):
    signed_in("director")
    assert client.get("/equipment/CE-NOPE/edit/", **HX).status_code == 404
    assert client.post("/equipment/CE-NOPE/status/", {"to": AssetStatus.OUT_OF_SERVICE}, **HX).status_code == 404


def test_another_facilitys_device_is_a_404_for_every_action(client, signed_in, vent, theirs):
    signed_in("director")
    tag = theirs["asset"].tag
    assert client.get(f"/equipment/{tag}/edit/", **HX).status_code == 404
    assert client.post(f"/equipment/{tag}/edit/", {"notes": "x", "room": "1"}, **HX).status_code == 404
    for to in (AssetStatus.OUT_OF_SERVICE, AssetStatus.RETIRED, AssetStatus.IN_SERVICE):
        assert client.post(f"/equipment/{tag}/status/", {"to": to}, **HX).status_code == 404
    with tenant_context(theirs["asset"].tenant):
        a = Asset.objects.get(tag=tag)
        assert (a.status, a.room, a.notes) == (AssetStatus.IN_SERVICE, "", "")


# --- status buttons ------------------------------------------------------------------------------------------------------

def drawer_buttons(body: str) -> dict[str, str]:
    """The drawer footer's status buttons: {label: the status it posts}."""
    out = {}
    for chunk in body.split('hx-post="/equipment/')[1:]:
        to = chunk.split('{"to": "', 1)[1].split('"', 1)[0]
        label = chunk.split(">", 1)[1].split("</button>", 1)[0]
        out[label] = to
    return out


def test_status_buttons_follow_the_status_and_the_role(client, signed_in, vent, make_user):
    signed_in("technician")
    url = f"/equipment/{vent.tag}/"
    assert drawer_buttons(client.get(url, **HX).content.decode()) == {
        "Tag out of service": AssetStatus.OUT_OF_SERVICE, "Lend out": AssetStatus.ON_LOAN, "Mark missing": AssetStatus.MISSING}
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.OUT_OF_SERVICE)
    assert drawer_buttons(client.get(url, **HX).content.decode()) == {"Return to service": AssetStatus.IN_SERVICE, "Mark missing": AssetStatus.MISSING}
    client.force_login(make_user("director"))  # Approve: Retire too
    assert drawer_buttons(client.get(url, **HX).content.decode()) == {
        "Return to service": AssetStatus.IN_SERVICE, "Mark missing": AssetStatus.MISSING, "Retire": AssetStatus.RETIRED}
    requester = make_user("requester")  # Equipment View: none
    requester.department = "ICU"  # slice 16: a requester sees their own unit's devices
    requester.save()
    client.force_login(requester)
    r = client.get(url, **HX)
    assert r.status_code == 200 and drawer_buttons(r.content.decode()) == {}


def test_tag_out_and_return_to_service(client, signed_in, vent):
    signed_in("technician")
    r = client.post(status_url(vent), {"to": AssetStatus.OUT_OF_SERVICE}, **HX)
    vent.refresh_from_db()
    assert r.status_code == 200 and vent.status == AssetStatus.OUT_OF_SERVICE and "HX-Retarget" not in r
    t = triggers(r)
    assert t["toast"] == {"value": "CE-10001 tagged out of service"} and "devices-changed" in t and "wo-changed" in t
    body = r.content.decode()
    assert "<h2>CE-10001 · ICU ventilator</h2>" in body and "Return to service" in drawer_buttons(body)
    r = client.post(status_url(vent), {"to": AssetStatus.IN_SERVICE}, **HX)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and triggers(r)["toast"] == {"value": "CE-10001 returned to service"}
    assert [h.history_change_reason for h in vent.history.all()[:2]] == ["Status: In service", "Status: Out of service"]


def test_a_status_change_keeps_the_tab_the_drawer_was_on(client, signed_in, vent):
    signed_in("technician")
    r = client.post(status_url(vent, tab="wo"), {"to": AssetStatus.OUT_OF_SERVICE}, **HX)
    assert active_tab("wo") in r.content.decode()
    r = client.post(status_url(vent), {"to": AssetStatus.IN_SERVICE}, **HX)
    assert active_tab("overview") in r.content.decode()


@pytest.mark.parametrize("start, to, message", [
    (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN, "CE-10001 lent out"),
    (AssetStatus.ON_LOAN, AssetStatus.IN_SERVICE, "CE-10001 back from loan and in service"),
    (AssetStatus.ON_LOAN, AssetStatus.OUT_OF_SERVICE, "CE-10001 tagged out of service"),
    (AssetStatus.IN_SERVICE, AssetStatus.MISSING, "CE-10001 marked missing"),
    (AssetStatus.MISSING, AssetStatus.IN_SERVICE, "CE-10001 found and back in service"),
    (AssetStatus.IN_REPAIR, AssetStatus.IN_SERVICE, "CE-10001 returned to service"),
])
def test_everyday_status_changes_at_equipment_edit(client, signed_in, vent, start, to, message):
    Asset.objects.filter(pk=vent.pk).update(status=start)
    signed_in("technician")
    r = client.post(status_url(vent), {"to": to}, **HX)
    vent.refresh_from_db()
    assert vent.status == to and triggers(r)["toast"] == {"value": message}


def test_retire_is_refused_to_a_technician_even_without_the_button(client, signed_in, vent):
    signed_in("technician")
    assert "Retire" not in drawer_buttons(client.get(f"/equipment/{vent.tag}/", **HX).content.decode())
    assert client.post(status_url(vent), {"to": AssetStatus.RETIRED}, **HX).status_code == 403
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and vent.next_pm_on is not None


def test_status_changes_need_equipment_edit(client, signed_in, vent):
    signed_in("requester")
    assert client.post(status_url(vent), {"to": AssetStatus.OUT_OF_SERVICE}, **HX).status_code == 403
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_the_director_retires_and_open_pm_work_is_cancelled(client, signed_in, vent):
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled PM")
    parts = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled PM")
    change_status(parts, WoStatus.AWAITING_PARTS)
    done = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Alarm")
    change_status(done, WoStatus.IN_PROGRESS)
    change_status(done, WoStatus.COMPLETED)
    signed_in("director")
    r = client.post(status_url(vent), {"to": AssetStatus.RETIRED}, **HX)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.RETIRED and vent.next_pm_on is None
    pm.refresh_from_db(), parts.refresh_from_db(), done.refresh_from_db()
    assert (pm.status, parts.status, done.status) == (WoStatus.CANCELLED, WoStatus.CANCELLED, WoStatus.COMPLETED)
    t = triggers(r)
    assert t["toast"] == {"value": "CE-10001 retired; 2 PM work orders cancelled"} and "devices-changed" in t and "wo-changed" in t
    assert drawer_buttons(r.content.decode()) == {"Reinstate": AssetStatus.IN_SERVICE}


def test_retiring_with_open_repair_work_shows_why_and_changes_nothing(client, signed_in, vent):
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority="high", problem="Low tidal volume alarm")
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled PM")
    signed_in("director")
    r = client.post(status_url(vent), {"to": AssetStatus.RETIRED}, **HX)
    assert r.status_code == 200
    t = triggers(r)
    assert t["toast"] == {"value": f"CE-10001 has open work: {repair.number}. Complete it, or cancel it (an in-progress work order goes "
                                     "back to open first), before retiring the device."}
    assert "devices-changed" not in t and "wo-changed" not in t
    vent.refresh_from_db(), pm.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and vent.next_pm_on is not None and pm.status == WoStatus.OPEN
    assert "Retire" in drawer_buttons(r.content.decode())  # the drawer as it was


def test_the_director_reinstates_a_retired_device(client, signed_in, vent):
    eq.set_status(vent, AssetStatus.RETIRED)
    signed_in("technician")
    assert drawer_buttons(client.get(f"/equipment/{vent.tag}/", **HX).content.decode()) == {}
    assert client.post(status_url(vent), {"to": AssetStatus.IN_SERVICE}, **HX).status_code == 403
    signed_in("director")
    r = client.post(status_url(vent), {"to": AssetStatus.IN_SERVICE}, **HX)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and vent.next_pm_on == TODAY
    assert triggers(r)["toast"] == {"value": "CE-10001 reinstated; its next PM is due today"}


def test_a_status_the_device_cannot_reach_is_a_toast(client, signed_in, vent):
    signed_in("technician")
    for to in (AssetStatus.IN_REPAIR, AssetStatus.IN_SERVICE):  # in repair comes from work orders; in service is where it is
        r = client.post(status_url(vent), {"to": to}, **HX)
        assert r.status_code == 200 and "cannot go from in service to" in triggers(r)["toast"]["value"] and "devices-changed" not in triggers(r)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_status_needs_a_known_status_and_a_post(client, signed_in, vent):
    signed_in("director")
    assert client.post(status_url(vent), {"to": "bogus"}, **HX).status_code == 400
    assert client.post(status_url(vent), {}, **HX).status_code == 400
    assert client.get(status_url(vent) + "?to=out_of_service", **HX).status_code == 405
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_status_toasts_cover_every_change_the_services_allow():
    for from_status, targets in eq.STATUS_CHANGES.items():
        for to in targets:
            assert status_toast("CE-1", from_status, to).startswith("CE-1 ")
    assert {to for to, _from in STATUS_TOASTS} == {to for targets in eq.STATUS_CHANGES.values() for to in targets}
    assert status_toast("CE-1", AssetStatus.IN_SERVICE, AssetStatus.RETIRED, 1) == "CE-1 retired; 1 PM work order cancelled"


# --- forms and queries -----------------------------------------------------------------------------------------------------

def test_forms_cover_what_the_services_take(ctx):
    """Edit details offers exactly update_asset's editable fields; Add device's new model fields are create_device_model's."""
    assert set(eq.EDITABLE_FIELDS) <= set(EditDeviceForm.base_fields)
    assert not {"tag", "status", "contract", "last_pm_on"} & set(EditDeviceForm.base_fields)
    params = set(inspect.signature(eq.create_device_model).parameters) - {"by"}
    assert set(MODEL_FIELDS) == params
    create = set(inspect.signature(eq.create_asset).parameters) - {"by", "today", "device_model", "department"}
    assert create <= set(NewDeviceForm.base_fields)


def test_layout_is_the_order_the_fields_are_on_screen(client, signed_in, vent, dept):
    """focus_first_error walks LAYOUT; it must list every field in the order the template shows them."""
    signed_in("technician")
    names = re.findall(r'name="(\w+)"', client.get(NEW_URL, **HX).content.decode())
    assert names == [n for n in LAYOUT if n in NewDeviceForm.base_fields] and set(NewDeviceForm.base_fields) == set(LAYOUT)
    names = re.findall(r'name="(\w+)"', client.get(f"/equipment/{vent.tag}/edit/", **HX).content.decode())
    assert names == [n for n in LAYOUT if n in EditDeviceForm.base_fields]


def test_a_form_sent_back_focuses_its_first_error(client, signed_in, vent, pump_model, dept):
    signed_in("technician")
    body = client.post(NEW_URL, new_post(pump_model, dept, installed_on="2024-05-01", warranty_end="2024-04-30"), **HX).content.decode()
    assert body.count("autofocus") == 1 and re.search(r'<input type="date" name="warranty_end"[^>]*autofocus', body)
    body = client.post(NEW_URL, new_post(None, dept, **new_model(manufacturer="")), **HX).content.decode()
    assert body.count("autofocus") == 1 and re.search(r'name="manufacturer"[^>]*autofocus', body)
    # Edit starts on the serial; sent back, on the field with the error
    assert re.search(r'name="serial"[^>]*autofocus', client.get(f"/equipment/{vent.tag}/edit/", **HX).content.decode())
    body = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, next_pm_on=""), **HX).content.decode()
    assert body.count("autofocus") == 1 and re.search(r'name="next_pm_on"[^>]*autofocus', body)


def test_the_modals_run_the_same_queries_for_two_models_or_twenty(client, signed_in, vent, pump):
    signed_in("technician")

    def count(url):
        with CaptureQueriesContext(connection) as q:
            assert client.get(url, **HX).status_code == 200
        return len(q.captured_queries)

    few = (count(NEW_URL), count(f"/equipment/{vent.tag}/edit/"))
    for i in range(18):
        Department.objects.create(name=f"Ward {i}")
        DeviceModel.objects.create(manufacturer="Acme", model=f"M{i}", description="Pump", category=f"Cat {i}")
    assert (count(NEW_URL), count(f"/equipment/{vent.tag}/edit/")) == few
