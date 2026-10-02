"""Contracts screen (slice 5): table, drawer, modal, the device drawer's support editor, permissions, and tenant isolation."""
from datetime import date, timedelta

import pytest

from apps.accounts.models import Level, Role, create_default_roles
from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, SupportType
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()


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
def contract(ctx, vent):
    c = ct.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, start_on=TODAY - timedelta(days=100),
                           end_on=TODAY + timedelta(days=265), annual_cost=5000, notes="Loaner within 24 h")
    ct.add_asset(c, vent)
    return c


@pytest.fixture
def theirs(tenant, other_tenant):
    """A contract, device model, and device that belong to the other tenant."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d)
        c = Contract.objects.create(reference="THEIRS-CT", vendor="X", start_on=TODAY, end_on=TODAY + timedelta(days=300))
        c.add_assets([asset])
    return c


def url(contract, action=""):
    return f"/contracts/{contract.pk}/{action}"


# --- page --------------------------------------------------------------------------------------------

def test_page_renders_kpis_table_and_filters(client, signed_in, contract, pump):
    signed_in("director")
    r = client.get("/contracts/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Service contracts" in body and "1 contract · 1 of 2 active devices covered" in body
    assert "Annual contract spend" in body and "$5,000" in body and "Devices under contract" in body and "Need a renewal decision" in body
    assert "SC-2026-118" in body and "Hamilton Medical Hamilton-G5" in body and "Full service" in body and "New contract" in body
    assert r.context["s"]["covered"] == 1 and round(r.context["s"]["annual_pct"], 1) == 12.1  # 5,000 of 41,200


def test_filters_and_partials(client, signed_in, contract):
    signed_in("analyst")
    old = ct.create_contract(reference="OLD-9", vendor="Steris", type=ContractType.THIRD_PARTY, start_on=TODAY - timedelta(days=400),
                             end_on=TODAY - timedelta(days=3))
    assert [c.reference for c in client.get("/contracts/?status=expired").context["page"]] == ["OLD-9"]
    assert [c.reference for c in client.get("/contracts/?type=oem&status=bogus").context["page"]] == ["SC-2026-118"]
    partial = client.get("/contracts/?q=steris", **hx("ct-body"))
    assert partial.status_code == 200 and b"<html" not in partial.content and b"OLD-9" in partial.content and b"SC-2026-118" not in partial.content
    assert b"No devices yet" in partial.content and f"Expired {ct.fmt_short(old.end_on)}".encode() in partial.content
    kpis = client.get("/contracts/", **hx("ct-kpis"))
    assert kpis.status_code == 200 and b"<html" not in kpis.content and b'id="ct-kpis"' in kpis.content and b"<table" not in kpis.content
    assert b"No contracts match these filters." in client.get("/contracts/?q=zzz", **hx("ct-body")).content


def test_covers_column_summarizes_models(client, signed_in, contract, dept, pump_model, vent, pump):
    signed_in("analyst")
    for i, (mfr, model) in enumerate([("A", "One"), ("B", "Two")]):
        dm = DeviceModel.objects.create(manufacturer=mfr, model=model, description="D", category="C")
        Asset.objects.create(tag=f"X-{i}", device_model=dm, department=dept, contract=contract)
        Asset.objects.create(tag=f"Y-{i}", device_model=dm, department=dept, contract=contract)
    ct.add_model(contract, pump_model)
    body = client.get("/contracts/", **hx("ct-body")).content.decode()
    assert "A One, B Two +2 more" in body and '<td class="num">6</td>' in body  # two largest groups first, tie broken by name


# --- drawer -------------------------------------------------------------------------------------------

def test_drawer_partial_and_full_page(client, signed_in, contract, vent):
    signed_in("technician")  # contracts: View
    partial = client.get(url(contract), **HX)
    assert partial.status_code == 200 and b"<html" not in partial.content
    body = partial.content.decode()
    assert "SC-2026-118 · Hamilton Medical" in body and "OEM service contract" in body and "Loaner within 24 h" in body and "1 device<" in body
    assert "$5,000 per year" in body and vent.tag in body and "Full service · Hamilton Medical Hamilton-G5" in body
    page = client.get(url(contract))
    assert page.status_code == 200 and b'class="drawer open"' in page.content and b"<html" in page.content


def test_drawer_hides_controls_below_edit_level(client, signed_in, contract):
    signed_in("analyst")
    body = client.get(url(contract), **HX).content.decode()
    for control in ("Edit contract", "Remove", "Renew 12 months", "Delete contract", "Add covered devices"):
        assert control not in body, control
    assert "New contract" not in client.get("/contracts/").content.decode()
    signed_in("manager")  # contracts: Edit
    body = client.get(url(contract), **HX).content.decode()
    for control in ("Edit contract", "Remove", "Renew 12 months", "Add covered devices", "Every active device is already on this contract."):
        assert control in body, control
    assert "Delete contract" not in body
    signed_in("director")
    assert "Delete contract" in client.get(url(contract), **HX).content.decode()


def test_view_level_gets_403_on_every_change(client, signed_in, contract, vent):
    signed_in("analyst")
    for action, data in [("save/", {}), ("renew/", {}), ("delete/", {}), ("add/", {"asset": vent.tag}), ("add-model/", {}), ("remove/", {"asset": vent.tag})]:
        assert client.post(url(contract, action), data, **HX).status_code == 403, action
    assert client.get("/contracts/new/", **HX).status_code == 403
    assert client.get(url(contract, "devices/?asset_q=CE"), **HX).status_code == 403
    assert client.post(f"/contracts/assets/{vent.tag}/support/", {"contract": ""}, **HX).status_code == 403


def test_changes_need_post(client, signed_in, contract):
    signed_in("director")
    for action in ("save/", "renew/", "delete/", "add/", "add-model/", "remove/"):
        assert client.get(url(contract, action)).status_code == 405, action


def test_other_tenants_contracts_are_not_found(client, make_user, other_tenant, contract, theirs, vent):
    client.force_login(make_user("director", tenant_=other_tenant))
    assert client.get(url(contract), **HX).status_code == 404
    assert client.post(url(contract, "renew/"), **HX).status_code == 404
    assert client.post(url(contract, "add/"), {"asset": "THEIRS-1"}, **HX).status_code == 404
    assert client.post(f"/contracts/assets/{vent.tag}/support/", {"contract": ""}, **HX).status_code == 404
    assert [c.reference for c in client.get("/contracts/").context["page"]] == ["THEIRS-CT"]
    assert client.get(url(theirs), **HX).status_code == 200
    # And our side cannot pull their device onto our contract or pick their contract for our device.
    client.force_login(make_user("director"))
    assert client.get(url(theirs), **HX).status_code == 404
    r = client.post(url(contract, "add/"), {"asset": "THEIRS-1"}, **HX)
    assert r.status_code == 200 and "Choose a device" in r["HX-Trigger"]
    r = client.post(f"/contracts/assets/{vent.tag}/support/", {"contract": str(theirs.pk)}, **HX)
    assert "Choose a contract" in r["HX-Trigger"]
    vent.refresh_from_db()
    assert vent.contract == contract


def test_manager_adds_moves_and_removes_devices(client, signed_in, contract, pump):
    signed_in("manager")
    r = client.post(url(contract, "add/"), {"asset": pump.tag}, **HX)
    assert r.status_code == 200 and "contracts-changed" in r["HX-Trigger"] and f"{pump.tag} added to SC-2026-118" in r["HX-Trigger"]
    assert pump.tag.encode() in r.content and b"2 devices" in r.content
    other = ct.create_contract(reference="B", vendor="BD", start_on=TODAY, end_on=TODAY + timedelta(days=300))
    r = client.post(url(other, "add/"), {"asset": pump.tag}, **HX)
    assert f"{pump.tag} moved from SC-2026-118 to B" in r["HX-Trigger"]
    r = client.post(url(other, "remove/"), {"asset": pump.tag}, **HX)
    assert f"{pump.tag} removed from B" in r["HX-Trigger"]
    pump.refresh_from_db()
    assert pump.contract is None and pump.support_type == SupportType.IN_HOUSE
    r = client.post(url(other, "remove/"), {"asset": pump.tag}, **HX)
    assert r.status_code == 200 and "is not on B" in r["HX-Trigger"] and "contracts-changed" not in r["HX-Trigger"]
    assert client.post(url(other, "remove/"), {"asset": "NOPE"}, **HX).status_code == 404


def test_add_every_device_of_a_model(client, signed_in, contract, dept, pump_model, pump):
    signed_in("manager")
    Asset.objects.create(tag="CE-3", device_model=pump_model, department=dept)
    drawer = client.get(url(contract), **HX).content.decode()
    assert "BD Alaris 8015 PCU · 2 not on this contract" in drawer
    r = client.post(url(contract, "add-model/"), {"device_model": str(pump_model.id)}, **HX)
    assert "2 devices added to SC-2026-118" in r["HX-Trigger"] and b"Every active device is already on this contract." in r.content
    assert b"BD Alaris 8015 PCU (2), Hamilton Medical Hamilton-G5 (1)" in r.content
    r = client.post(url(contract, "add-model/"), {"device_model": "junk"}, **HX)
    assert r.status_code == 200 and "Choose a device model" in r["HX-Trigger"]


def test_device_picker(client, signed_in, contract, vent, pump):
    signed_in("manager")
    assert client.get(url(contract, "devices/?asset_q=C"), **HX).content == b""
    picks = client.get(url(contract, "devices/?asset_q=CE-"), **HX).content.decode()
    assert pump.tag in picks and vent.tag not in picks and url(contract, "add/") in picks  # vent is already on it
    assert "No devices match" in client.get(url(contract, "devices/?asset_q=zzz"), **HX).content.decode()


def test_covered_device_list_filter_and_cap(client, signed_in, contract, dept, pump_model):
    signed_in("technician")
    for i in range(42):
        Asset.objects.create(tag=f"P-{i:03}", device_model=pump_model, department=dept, contract=contract)
    drawer = client.get(url(contract), **HX).content.decode()
    assert 'id="ct-filter"' in drawer and "Showing 40 of 43" in drawer
    lst = client.get(url(contract, "?dq=g5"), **hx("ct-list"))
    body = lst.content.decode()
    assert lst.status_code == 200 and body.lstrip().startswith('<div id="ct-list">')
    assert "CE-10001" in body and "P-001" not in body and "1 of 43 devices match" in body
    assert "No covered devices match the filter." in client.get(url(contract, "?dq=zzz"), **hx("ct-list")).content.decode()
    # The device filter does not leak into the table's search when the drawer is opened as a page.
    assert client.get(url(contract, "?dq=zzz")).context["f"].q == ""


def test_inline_edit_and_save(client, signed_in, contract):
    signed_in("manager")
    form = client.get(url(contract, "?edit=1"), **HX).content.decode()
    assert 'name="reference"' in form and 'value="SC-2026-118"' in form and "Edit contract</h3>" in form
    data = {"reference": "SC-2026-118", "vendor": "Hamilton Medical", "type": "third_party", "coverage": "parts_labor", "start_on": TODAY.isoformat(),
            "end_on": (TODAY + timedelta(days=400)).isoformat(), "annual_cost": "6200", "notes": ""}
    r = client.post(url(contract, "save/"), data, **HX)
    assert r.status_code == 200 and "SC-2026-118 updated" in r["HX-Trigger"] and b"Third-party service agreement" in r.content
    contract.refresh_from_db()
    assert contract.coverage == "parts_labor" and contract.annual_cost == 6200
    bad = client.post(url(contract, "save/"), {**data, "end_on": (TODAY - timedelta(days=1)).isoformat()}, **HX)
    assert bad.status_code == 200 and "on or after the start" in bad.content.decode() and "HX-Trigger" not in bad
    blank = client.post(url(contract, "save/"), {**data, "reference": ""}, **HX)
    assert "required" in blank.content.decode() and 'name="vendor"' in blank.content.decode()  # the edit form stays open with the messages
    contract.refresh_from_db()
    assert contract.reference == "SC-2026-118" and contract.end_on == TODAY + timedelta(days=400)
    ct.create_contract(reference="DUP", vendor="V", start_on=TODAY, end_on=TODAY + timedelta(days=10))
    dup = client.post(url(contract, "save/"), {**data, "reference": "dup"}, **HX)
    assert "already used by another contract" in dup.content.decode()
    # An analyst asking for the edit form gets the read-only drawer.
    signed_in("analyst")
    assert 'name="reference"' not in client.get(url(contract, "?edit=1"), **HX).content.decode()


def test_renew(client, signed_in, contract):
    signed_in("manager")
    r = client.post(url(contract, "renew/"), **HX)
    contract.refresh_from_db()
    assert r.status_code == 200 and f"SC-2026-118 renewed through {ct.fmt_date(contract.end_on)}" in r["HX-Trigger"] and "contracts-changed" in r["HX-Trigger"]
    assert contract.end_on == add_months(TODAY + timedelta(days=265), 12)


def test_only_full_level_deletes(client, signed_in, contract, vent):
    signed_in("manager")
    assert client.post(url(contract, "delete/"), **HX).status_code == 403
    signed_in("director")
    r = client.post(url(contract, "delete/"), **HX)
    assert r.status_code == 200 and r.content == b"" and "SC-2026-118 deleted; 1 device set to in-house support" in r["HX-Trigger"]
    assert "contracts-changed" in r["HX-Trigger"] and not Contract.objects.exists()
    vent.refresh_from_db()
    assert vent.contract is None and vent.support_type == SupportType.IN_HOUSE


# --- new contract modal -------------------------------------------------------------------------------------

def test_new_contract_modal_prefills_from_a_device_and_attaches_it(client, signed_in, pump):
    signed_in("manager")
    form = client.get(f"/contracts/new/?asset={pump.tag}", **HX)
    assert form.status_code == 200
    body = form.content.decode()
    assert 'value="BD"' in body and 'value="200"' in body and f"{pump.tag} · Infusion pump will be added to this contract." in body  # 7% of 3,200 → 224 → 200
    assert f'value="{TODAY.isoformat()}"' in body and f'value="{add_months(TODAY, 12).isoformat()}"' in body
    data = {"asset": pump.tag, "reference": "SC-NEW", "vendor": "BD", "type": "oem", "coverage": "full", "start_on": TODAY.isoformat(),
            "end_on": add_months(TODAY, 12).isoformat(), "annual_cost": "200", "notes": ""}
    r = client.post("/contracts/new/", data, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and "SC-NEW created" in r["HX-Trigger"] and "modal-close" in r["HX-Trigger-After-Settle"]
    c = Contract.objects.get(reference="SC-NEW")
    pump.refresh_from_db()
    assert pump.contract == c and pump.support_type == SupportType.OEM_CONTRACT and pump.tag.encode() in r.content


def test_new_contract_modal_without_a_device(client, signed_in):
    signed_in("director")
    body = client.get("/contracts/new/", **HX).content.decode()
    assert "New service contract" in body and "Add covered devices after the contract is created" in body and 'name="asset"' not in body


def test_new_contract_validation_errors_re_render_the_modal(client, signed_in, contract):
    signed_in("manager")
    data = {"reference": "SC-2026-118", "vendor": "V", "type": "oem", "coverage": "full", "start_on": TODAY.isoformat(), "end_on": TODAY.isoformat(),
            "annual_cost": "0", "notes": ""}
    r = client.post("/contracts/new/", data, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "New service contract" in body and "already used by another contract" in body and "HX-Retarget" not in r
    body = client.post("/contracts/new/", {**data, "reference": "SC-2", "vendor": "", "annual_cost": "-1"}, **HX).content.decode()
    assert "required" in body and "greater than or equal to 0" in body
    assert Contract.objects.count() == 1


# --- device drawer: support editor -------------------------------------------------------------------------

def test_device_drawer_links_the_contract_and_offers_edit_by_level(client, signed_in, contract, vent):
    signed_in("technician")  # contracts: View
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert f'href="{url(contract)}"' in body and 'id="sp-host"' in body and f"/contracts/assets/{vent.tag}/support/" not in body
    requester = signed_in("requester")  # contracts: None
    requester.department = "ICU"  # slice 16: a requester sees their own unit's devices
    requester.save()
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "SC-2026-118 · Hamilton Medical" in body and f'href="{url(contract)}"' not in body
    signed_in("manager")
    assert f"/contracts/assets/{vent.tag}/support/" in client.get(f"/equipment/{vent.tag}/", **HX).content.decode()


def test_support_editor_changes_the_contract(client, signed_in, contract, vent, pump):
    signed_in("manager")
    form = client.get(f"/contracts/assets/{pump.tag}/support/", **HX).content.decode()
    assert "In-house, no contract" in form and "SC-2026-118 · Hamilton Medical · Full service" in form and "New contract for this device" in form
    r = client.post(f"/contracts/assets/{pump.tag}/support/", {"contract": str(contract.pk)}, **HX)
    assert r.status_code == 200 and f"{pump.tag} added to SC-2026-118" in r["HX-Trigger"] and "contracts-changed" in r["HX-Trigger"]
    assert b"Under contract" in r.content and pump.tag.encode() in r.content  # the device drawer, re-rendered
    pump.refresh_from_db()
    assert pump.contract == contract
    r = client.post(f"/contracts/assets/{vent.tag}/support/", {"contract": ""}, **HX)
    assert f"{vent.tag} set to in-house support" in r["HX-Trigger"]
    vent.refresh_from_db()
    assert vent.contract is None and vent.support_type == SupportType.IN_HOUSE
    r = client.post(f"/contracts/assets/{vent.tag}/support/", {"contract": "not-a-uuid"}, **HX)
    assert r.status_code == 200 and "Choose a contract from the list." in r["HX-Trigger"]


def test_support_editor_marks_expired_contracts(client, signed_in, vent):
    signed_in("director")
    ct.create_contract(reference="OLD", vendor="V", start_on=TODAY - timedelta(days=400), end_on=TODAY - timedelta(days=1))
    assert "OLD · V · Full service (expired)" in client.get(f"/contracts/assets/{vent.tag}/support/", **HX).content.decode()


def test_support_editor_needs_sign_in(client, db, vent):
    r = client.get(f"/contracts/assets/{vent.tag}/support/")
    assert r.status_code == 302 and r["Location"].startswith("/login/")


def test_roles_without_contracts_access_get_403(client, signed_in, contract):
    for slug in ("requester", "vendor"):
        signed_in(slug)
        assert client.get("/contracts/").status_code == 403, slug
        assert client.get(url(contract), **HX).status_code == 403, slug


def test_other_tenants_contract_changes_are_not_found(client, make_user, other_tenant, contract, theirs, vent):
    client.force_login(make_user("director", tenant_=other_tenant))
    for action, data in (("save/", {}), ("delete/", {}), ("renew/", {}), ("add/", {"asset": vent.tag}), ("remove/", {"asset": vent.tag}),
                         ("add-model/", {"device_model": str(vent.device_model_id)})):
        assert client.post(url(contract, action), data, **HX).status_code == 404, action
    assert client.get(url(contract, "devices/?asset_q=CE"), **HX).status_code == 404
    # and our director cannot pull the other tenant's device or model onto our contract
    client.force_login(make_user("director"))
    r = client.post(url(contract, "add/"), {"asset": "THEIRS-1"}, **HX)
    assert r.status_code == 200 and "Choose a device" in r["HX-Trigger"]
    foreign_model = Asset.unscoped.get(tag="THEIRS-1").device_model  # unscoped: the test reaches across tenants on purpose
    r = client.post(url(contract, "add-model/"), {"device_model": str(foreign_model.id)}, **HX)
    assert r.status_code == 200 and "Choose a device model" in r["HX-Trigger"]
    assert Asset.unscoped.get(tag="THEIRS-1").contract_id == theirs.id


def test_support_editor_needs_equipment_view_even_with_contracts_edit(client, ctx, make_user, vent):
    from apps.accounts import services as accounts

    clerk = accounts.create_role(name="Contracts clerk", copy_from=Role.objects.get(slug="analyst"))
    accounts.set_role_level(clerk, "contracts", Level.EDIT)
    accounts.set_role_level(clerk, "equipment", Level.NONE)
    user = make_user("analyst", username="clerk@riverside.example")
    user.role = clerk
    user.save()
    client.force_login(user)
    assert client.get(f"/contracts/assets/{vent.tag}/support/", **HX).status_code == 403


def test_retired_devices_are_rejected_by_the_drawer_and_the_support_editor(client, signed_in, contract, pump):
    signed_in("manager")
    pump.status = AssetStatus.RETIRED
    pump.save()
    r = client.post(url(contract, "add/"), {"asset": pump.tag}, **HX)
    pump.refresh_from_db()
    assert r.status_code == 200 and pump.contract_id is None and "retired" in r["HX-Trigger"]
    r = client.post(f"/contracts/assets/{pump.tag}/support/", {"contract": str(contract.pk)}, **HX)
    pump.refresh_from_db()
    assert r.status_code == 200 and pump.contract_id is None and "retired" in r["HX-Trigger"]
