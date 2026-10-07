"""
Slice 25, part D: how a device was added (Asset.added_as, which the survey binder's incoming-inspection section reads), the doors. Add
device's choice of how the device arrives (slice 26, replacing slice 25's "Already in use here" box: new, waiting for its incoming
inspection, or already in use here), the API's device create ("new" by default, "existing"; "imported" refused in words; never changed
afterwards), the devices import (imported; a device already here keeps how it was added), and the History tab's words.
"""
import json
import re
from io import StringIO

import pytest
from django.core.management import call_command
from pg_helpers import as_app_role, needs_postgres

from apps.core.history import record_history
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, RiskClass
from apps.imports import services as imports
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context
from apps.workorders.models import WoType

HX = {"HTTP_HX_REQUEST": "true"}
NEW_URL = "/equipment/new/"
ASSETS = "/api/v1/assets/"


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def new_post(dm, dept, **over) -> dict:
    """Add device's post: a new device, waiting for its incoming inspection (slice 26's default choice), unless `intake` says otherwise."""
    data = {"tag": "CE-20001", "serial": "SN-77", "device_model": str(dm.pk), "department": str(dept.pk), "room": "412", "installed_on": "",
            "acquisition_cost": "", "warranty_end": "", "condition": "4", "intake": "waiting", "inspection_due": "", "last_pm_on": "",
            "next_pm_on": "", "status": AssetStatus.IN_SERVICE, "notes": "", "risk_class": RiskClass.MEDIUM, "oem_pm_interval_months": "12",
            "expected_life_years": "8", "list_cost": "", "manufacturer": "", "model": "", "description": "", "category": "", "new_department": ""}
    data.update(over)
    return data


def existing(**over) -> dict:
    """Add device's "Already in use here (existing equipment)": its install date is required."""
    return {"intake": "existing", "installed_on": "2020-03-01", **over}


def intake_radio(body: str, value: str) -> str:
    return re.search(rf'<input type="radio" name="intake" value="{value}"[^>]*>', body).group(0)


# --- Add device ----------------------------------------------------------------------------------------------------------------


def test_add_device_asks_how_the_device_arrives_and_edit_details_does_not(client, signed_in, vent):
    """Slice 26: one choice replaces slice 25's box. New and waiting for its incoming inspection is the default; existing equipment is
    said so, and the survey binder asks about one installed recently."""
    signed_in("technician")
    body = client.get(NEW_URL, **HX).content.decode()
    assert "checked" in intake_radio(body, "waiting") and "checked" not in intake_radio(body, "existing")
    assert "New: waiting for its incoming inspection" in body and "Already in use here (existing equipment)" in body
    assert "One installed in the last 30 days is probably new, and the survey binder asks about it." in body
    assert 'name="already_in_use"' not in body
    assert 'name="intake"' not in client.get(f"/equipment/{vent.tag}/edit/", **HX).content.decode()


def test_a_device_added_on_screen_is_new_unless_already_in_use_here_is_chosen(client, signed_in, vent_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(vent_model, dept), **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"]["value"].startswith("CE-20001 added; WO-")
    r = client.post(NEW_URL, new_post(vent_model, dept, tag="CE-20002", **existing()), **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"]["value"] == "CE-20002 added"
    assert dict(Asset.objects.values_list("tag", "added_as")) == {"CE-20001": AddedAs.NEW, "CE-20002": AddedAs.EXISTING}
    assert dict(Asset.objects.values_list("tag", "awaiting_inspection")) == {"CE-20001": True, "CE-20002": False}
    assert list(Asset.objects.get(tag="CE-20001").work_orders.values_list("type", flat=True)) == [WoType.INSPECTION]
    assert not Asset.objects.get(tag="CE-20002").work_orders.exists()


def test_existing_equipment_needs_its_install_date(client, signed_in, vent_model, dept):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(vent_model, dept, **existing(installed_on="")), **HX)
    body = r.content.decode()
    assert "Enter the install date of equipment already in use here." in body.split('id="dv-installed_on_error">', 1)[1].split("</span>", 1)[0]
    assert not Asset.objects.exists()
    # a new device's install date stays optional
    assert client.post(NEW_URL, new_post(vent_model, dept), **HX)["HX-Retarget"] == "#drawer"
    assert Asset.objects.get(tag="CE-20001").installed_on is None


def test_a_refused_device_keeps_the_choice_as_it_was(client, signed_in, vent_model, dept, vent):
    signed_in("technician")
    r = client.post(NEW_URL, new_post(vent_model, dept, tag=vent.tag, **existing()), **HX)  # the tag is taken
    body = r.content.decode()
    assert "checked" in intake_radio(body, "existing") and "checked" not in intake_radio(body, "waiting") and Asset.objects.count() == 1


def test_the_history_tab_reads_how_a_device_was_added(ctx, vent_model, dept, make_user):
    new = eq.create_asset(tag="H-1", device_model=vent_model, department=dept, by=make_user("director"))
    existing = eq.create_asset(tag="H-2", device_model=vent_model, department=dept, added_as=AddedAs.EXISTING)
    for asset, words in ((new, "New to the facility"), (existing, "Already in use here")):
        entries, _more = record_history(asset)
        assert {c.field: c.after for c in entries[-1].changes}["Added as"] == words


def test_the_device_drawers_history_tab_shows_it(client, signed_in, vent_model, dept):
    signed_in("director")
    client.post(NEW_URL, new_post(vent_model, dept, **existing()), **HX)
    body = client.get("/equipment/CE-20001/?tab=history", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="drawer").content.decode()
    assert "Added as" in body and "Already in use here" in body
    client.post(NEW_URL, new_post(vent_model, dept, tag="CE-20002"), **HX)
    body = client.get("/equipment/CE-20002/?tab=history", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="drawer").content.decode()
    assert "Added as" in body and "New to the facility" in body


# --- the API -------------------------------------------------------------------------------------------------------------------


def post(client, url, body):
    return client.post(url, body, content_type="application/json")


def body_for(vent_model, dept, **over) -> dict:
    return {"tag": "CE-40001", "device_model": str(vent_model.pk), "department": str(dept.pk), **over}


def test_the_api_adds_a_new_device_by_default_or_one_already_in_use(client, signed_in, vent_model, dept):
    signed_in("technician")
    r = post(client, ASSETS, body_for(vent_model, dept))
    assert r.status_code == 201 and (r.json()["added_as"], r.json()["added_as_label"]) == ("new", "New to the facility")
    r = post(client, ASSETS, body_for(vent_model, dept, tag="CE-40002", added_as="existing"))
    assert r.status_code == 201 and (r.json()["added_as"], r.json()["added_as_label"]) == ("existing", "Already in use here")
    r = post(client, ASSETS, body_for(vent_model, dept, tag="CE-40003", added_as=""))
    assert r.status_code == 201 and r.json()["added_as"] == "new"
    assert dict(Asset.objects.values_list("tag", "added_as")) == {"CE-40001": "new", "CE-40002": "existing", "CE-40003": "new"}


@pytest.mark.parametrize("value, words", [("imported", "Imported devices come in through Settings, Import data"), ("borrowed", "not a valid choice")])
def test_the_api_refuses_what_a_device_added_there_cannot_be(client, signed_in, vent_model, dept, value, words):
    signed_in("technician")
    r = post(client, ASSETS, body_for(vent_model, dept, added_as=value))
    assert r.status_code == 400 and words in r.json()["added_as"][0]
    assert not Asset.objects.exists()


def test_how_a_device_was_added_never_changes_through_the_api(client, signed_in, vent_model, dept, vent):
    signed_in("technician")
    added = eq.create_asset(tag="CE-40004", device_model=vent_model, department=dept)
    r = client.patch(f"{ASSETS}{added.pk}/", {"added_as": "existing"}, content_type="application/json")
    assert r.status_code == 400 and "never changes" in r.json()["added_as"][0]
    added.refresh_from_db()
    assert added.added_as == AddedAs.NEW
    data = client.get(f"{ASSETS}{added.pk}/").json()
    r = client.patch(f"{ASSETS}{added.pk}/", {"added_as": data["added_as"], "added_as_label": data["added_as_label"], "room": "7"},
                     content_type="application/json")
    assert r.status_code == 200 and r.json()["room"] == "7"  # what a GET returned goes back fine
    old = client.get(f"{ASSETS}{vent.pk}/").json()  # added before slice 25 (or by the seed): nobody knows how
    assert (old["added_as"], old["added_as_label"]) == ("", "")
    assert client.patch(f"{ASSETS}{vent.pk}/", {"added_as": "", "room": "9"}, content_type="application/json").status_code == 200
    assert client.patch(f"{ASSETS}{vent.pk}/", {"added_as": "new"}, content_type="application/json").status_code == 400
    vent.refresh_from_db()
    assert vent.added_as == ""


# --- the devices import --------------------------------------------------------------------------------------------------------


def run_through(run, user):
    run = imports.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = imports.process(run, user)
    return run


def import_devices(user, *lines):
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    run = imports.upload(user, "devices", "inventory.csv", data)
    run = run_through(imports.confirm_columns(run, user, imports.mapping_of(run)), user)
    return run_through(imports.start_import(run, user), user)


def test_the_devices_import_adds_them_as_imported_and_keeps_how_one_already_here_was_added(ctx, make_user, vent_model, dept):
    director = make_user("director")
    here = eq.create_asset(tag="CE-50001", device_model=vent_model, department=dept, added_as=AddedAs.EXISTING)
    run = import_devices(director, "Asset Tag,Manufacturer,Model,Department,Room,Status",
                         "CE-50001,Hamilton Medical,Hamilton-G5,ICU,5,Active", "CE-50002,Hamilton Medical,Hamilton-G5,ICU,6,Active",
                         "CE-50003,Hamilton Medical,Hamilton-G5,ICU,7,Retired")
    assert run.status == "imported" and run.counts == {"create": 2, "update": 1}
    assert dict(Asset.objects.values_list("tag", "added_as")) == {"CE-50001": "existing", "CE-50002": "imported", "CE-50003": "imported"}
    here.refresh_from_db()
    assert here.room == "5"


def test_import_assets_adds_them_as_imported(ctx, dept, pump_model, tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("tag,manufacturer,model,department,room\nCE-60001,BD,Alaris 8015 PCU,ICU,2\n")
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=StringIO(), stderr=StringIO())
    assert Asset.objects.get(tag="CE-60001").added_as == AddedAs.IMPORTED


# --- under the policies --------------------------------------------------------------------------------------------------------


@needs_postgres
def test_adding_devices_under_the_policies(client, signed_in, vent_model, dept, tenant):
    """As the runtime role: Add device and the API's create record how each device was added in the facility the request set."""
    signed_in("technician")
    as_app_role()
    assert client.post(NEW_URL, new_post(vent_model, dept, **existing()), **HX).status_code == 200
    assert post(client, ASSETS, body_for(vent_model, dept)).json()["added_as"] == "new"
    with tenant_context(tenant):  # the requests restored the connection's facility setting when they finished
        assert dict(Asset.objects.values_list("tag", "added_as")) == {"CE-20001": "existing", "CE-40001": "new"}
