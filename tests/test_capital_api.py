"""
Slice 36, wave 2B in the API: a device model's expected life source and manufacturer support (life_source, sale_ended_on,
support_ends_on, parts_end_on, support_notice, support_checked_on) read on /api/v1/device-models/ with a read-only support_status
({"key", "words"} for the facility's today), changed with PATCH or PUT through update_device_model at Equipment Edit (each bad value a
400 keyed by its field, nothing saved), refused when adding a model; a device's device_model_detail carries them too. Settings'
fiscal_year_start_month through PATCH /api/v1/settings/ (1 to 12). The day is pinned (the API reads the facility's clock).
"""
from datetime import date

import pytest
from django.utils import timezone

from apps.core.history import record_history
from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel, LifeSource
from apps.facility import services as fs
from apps.tenants.context import tenant_context

TODAY = date(2026, 10, 19)
MODELS = "/api/v1/device-models/"
SETTINGS = "/api/v1/settings/"
SUPPORT = ("life_source", "sale_ended_on", "support_ends_on", "parts_end_on", "support_notice", "support_checked_on")
NOTHING = {"life_source": "", "sale_ended_on": None, "support_ends_on": None, "parts_end_on": None, "support_notice": "", "support_checked_on": None}


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    real = timezone.localdate
    monkeypatch.setattr(timezone, "localdate", lambda value=None, tz=None: TODAY if value is None else real(value, tz))


def one(dm) -> str:
    return f"{MODELS}{dm.pk}/"


def patch(client, url, body):
    return client.patch(url, body, content_type="application/json")


def support_of(data: dict) -> dict:
    return {f: data[f] for f in SUPPORT}


def stored(dm) -> dict:
    dm = DeviceModel.objects.get(pk=dm.pk)
    return {f: getattr(dm, f) for f in SUPPORT}


# --- reading -------------------------------------------------------------------------------------------------------------------------

def test_a_model_reads_its_support_and_the_status_today(client, ctx, make_user, vent_model):
    client.force_login(make_user("analyst"))  # Equipment View reads
    data = client.get(one(vent_model)).json()
    assert support_of(data) == NOTHING
    assert data["support_status"] == {"key": "unknown", "words": "Support not checked"}
    eq.update_device_model(vent_model, life_source=LifeSource.MANUFACTURER, sale_ended_on=date(2024, 1, 1), support_ends_on=date(2026, 10, 1),
                           parts_end_on=date(2029, 6, 30), support_notice="FSN-2025-014", support_checked_on=date(2026, 9, 2))
    data = client.get(one(vent_model)).json()
    assert support_of(data) == {"life_source": "manufacturer", "sale_ended_on": "2024-01-01", "support_ends_on": "2026-10-01",
                                "parts_end_on": "2029-06-30", "support_notice": "FSN-2025-014", "support_checked_on": "2026-09-02"}
    assert data["support_status"] == {"key": "support_ended", "words": "Manufacturer support ended Oct 1, 2026"}
    listed = next(m for m in client.get(MODELS).json()["results"] if m["id"] == str(vent_model.pk))
    assert listed["support_status"]["key"] == "support_ended"


def test_a_device_carries_its_models_support(client, ctx, make_user, vent, vent_model):
    eq.update_device_model(vent_model, parts_end_on=date(2026, 6, 1))
    client.force_login(make_user("analyst"))
    detail = client.get(f"/api/v1/assets/{vent.pk}/").json()["device_model_detail"]
    assert detail["parts_end_on"] == "2026-06-01"
    assert detail["support_status"] == {"key": "parts_ended", "words": "Parts no longer available since Jun 1, 2026"}


# --- changing it ------------------------------------------------------------------------------------------------------------------------

def test_patch_records_support_through_the_service(client, ctx, make_user, vent_model):
    user = make_user("technician")  # Equipment Edit by default
    client.force_login(user)
    r = patch(client, one(vent_model), {"life_source": "aha", "support_ends_on": "2028-03-31", "support_notice": " EOL-77 ",
                                        "support_checked_on": "2026-10-12"})
    assert r.status_code == 200, r.content
    data = r.json()
    assert support_of(data) == {**NOTHING, "life_source": "aha", "support_ends_on": "2028-03-31", "support_notice": "EOL-77",
                                "support_checked_on": "2026-10-12"}
    assert data["support_status"] == {"key": "announced", "words": "Support ends Mar 31, 2028"}
    assert stored(vent_model)["support_ends_on"] == date(2028, 3, 31)
    entries, _ = record_history(DeviceModel.objects.get(pk=vent_model.pk))
    changes = {c.field: c.after for c in entries[0].changes}
    assert changes["Expected life from"] == "AHA estimated useful lives" and changes["Manufacturer's notice"] == "EOL-77"
    assert entries[0].who == str(user)
    # cleared with blanks and nulls
    r = patch(client, one(vent_model), {"life_source": "", "support_ends_on": None, "support_notice": "", "support_checked_on": None})
    assert r.status_code == 200 and support_of(r.json()) == NOTHING and r.json()["support_status"]["key"] == "unknown"


def test_put_sends_back_what_get_returned(client, ctx, make_user, vent_model):
    eq.update_device_model(vent_model, life_source=LifeSource.ECRI, parts_end_on=date(2030, 1, 1), support_checked_on=date(2026, 1, 5))
    client.force_login(make_user("director"))
    body = client.get(one(vent_model)).json()
    body["support_status"] = {"key": "supported", "words": "anything"}  # read only: ignored
    r = client.put(one(vent_model), body, content_type="application/json")
    assert r.status_code == 200, r.content
    assert stored(vent_model) == {**NOTHING, "life_source": "ecri", "parts_end_on": date(2030, 1, 1), "support_checked_on": date(2026, 1, 5)}
    assert r.json()["support_status"] == {"key": "announced", "words": "Parts available until Jan 1, 2030"}
    # a PUT that changes one support field saves it with the rest
    body = {**client.get(one(vent_model)).json(), "support_ends_on": "2027-12-31"}
    assert client.put(one(vent_model), body, content_type="application/json").status_code == 200
    assert stored(vent_model)["support_ends_on"] == date(2027, 12, 31)


@pytest.mark.parametrize("field, bad", [
    ("life_source", "guess"), ("sale_ended_on", "31/03/2028"), ("support_ends_on", "soon"), ("parts_end_on", 5),
    ("support_notice", "two words"), ("support_notice", "x" * 41), ("support_notice", ["FSN-1"]), ("support_checked_on", "2026-10-20"),
    ("support_checked_on", "2999-01-01"),
])
def test_patch_refuses_each_bad_value_keyed_by_field(client, ctx, make_user, vent_model, field, bad):
    client.force_login(make_user("director"))
    r = patch(client, one(vent_model), {"support_ends_on": "2028-03-31", field: bad})
    assert r.status_code == 400 and list(r.json()) == [field], r.content
    assert all(isinstance(m, str) and m for m in r.json()[field])
    assert stored(vent_model) == NOTHING  # all or nothing


def test_the_future_checked_on_says_why(client, ctx, make_user, vent_model):
    client.force_login(make_user("director"))
    r = patch(client, one(vent_model), {"support_checked_on": "2026-10-20"})
    assert r.json() == {"support_checked_on": ["The day support was checked cannot be after today."]}


def test_changing_support_needs_equipment_edit(client, ctx, make_user, vent_model):
    client.force_login(make_user("analyst"))  # Equipment View
    assert client.get(one(vent_model)).status_code == 200
    assert patch(client, one(vent_model), {"support_ends_on": "2028-03-31"}).status_code == 403
    client.force_login(make_user("requester"))  # Equipment View, scoped to a unit: read only
    assert patch(client, one(vent_model), {"life_source": "aha"}).status_code == 403
    assert stored(vent_model) == NOTHING


# --- adding a model -----------------------------------------------------------------------------------------------------------------

NEW = {"manufacturer": "Mindray", "model": "BeneVision N12", "description": "Patient monitor", "category": "Monitors", "risk_class": "high"}


def test_adding_a_model_refuses_the_support_fields(client, ctx, make_user):
    client.force_login(make_user("director"))
    sent = {"life_source": "aha", "sale_ended_on": "2024-01-01", "support_ends_on": "2028-03-31", "parts_end_on": "2030-01-01",
            "support_notice": "FSN-1", "support_checked_on": "2026-10-01"}
    r = client.post(MODELS, {**NEW, **sent}, content_type="application/json")
    assert r.status_code == 400 and set(r.json()) == set(SUPPORT), r.content
    assert r.json()["support_ends_on"] == ["Set this with PATCH once the model is added."]
    assert not DeviceModel.objects.filter(model="BeneVision N12").exists()
    # blanks are nothing sent: the model is added with none recorded
    r = client.post(MODELS, {**NEW, "life_source": "", "support_notice": "", "support_ends_on": None}, content_type="application/json")
    assert r.status_code == 201, r.content
    assert support_of(r.json()) == NOTHING and r.json()["support_status"] == {"key": "unknown", "words": "Support not checked"}


# --- the fiscal year in Settings -------------------------------------------------------------------------------------------------------

def test_settings_patch_saves_the_fiscal_year(client, ctx, make_user):
    client.force_login(make_user("director"))
    assert client.get(SETTINGS).json()["fiscal_year_start_month"] == 1
    r = patch(client, SETTINGS, {"fiscal_year_start_month": 7})
    assert r.status_code == 200 and r.json()["fiscal_year_start_month"] == 7
    assert fs.get_settings().fiscal_year_start_month == 7
    assert patch(client, SETTINGS, client.get(SETTINGS).json()).status_code == 200  # what GET returned goes back unchanged
    assert patch(client, SETTINGS, {"fiscal_year_start_month": "10"}).json()["fiscal_year_start_month"] == 10


@pytest.mark.parametrize("bad", [13, 0, -1, "July", "7.5", 7.5, True, None, ""])
def test_settings_patch_refuses_a_month_that_is_not_one(client, ctx, make_user, bad):
    fs.update_settings(fiscal_year_start_month=7)
    client.force_login(make_user("director"))
    r = patch(client, SETTINGS, {"fiscal_year_start_month": bad})
    assert r.status_code == 400 and list(r.json()) == ["fiscal_year_start_month"], r.content
    assert fs.get_settings().fiscal_year_start_month == 7


def test_settings_patch_of_the_fiscal_year_needs_settings_edit(client, ctx, make_user):
    client.force_login(make_user("manager"))  # Settings View
    assert client.get(SETTINGS).json()["fiscal_year_start_month"] == 1
    assert patch(client, SETTINGS, {"fiscal_year_start_month": 7}).status_code == 403
    assert fs.get_settings().fiscal_year_start_month == 1


def test_another_facilitys_model_and_fiscal_year_stay_theirs(client, ctx, make_user, other_tenant):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
    client.force_login(make_user("director"))
    assert client.get(one(theirs)).status_code == 404
    assert patch(client, one(theirs), {"support_ends_on": "2028-03-31"}).status_code == 404
    patch(client, SETTINGS, {"fiscal_year_start_month": 7})
    with tenant_context(other_tenant):
        assert DeviceModel.objects.get(pk=theirs.pk).support_ends_on is None
        assert fs.get_settings().fiscal_year_start_month == 1
    assert Asset.objects.count() == 0
