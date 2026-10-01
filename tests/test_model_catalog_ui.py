"""The device model catalog in the web UI (slice 14, part A): the model drawer's PM program tab, Add model, Edit details, and the
risk score. Permissions are checked server-side on GET and POST (PM View for the screen, then Equipment Edit to add and edit,
Equipment Approve to score), another facility's model is a 404, changes go through apps.equipment.services, and every save
swaps the model's drawer into #drawer, toasts, fires models-changed, and closes the modal after settle. Also the device drawer's
risk row, the Settings summary, and the API's risk-class rule."""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
NEW_URL = "/pm/models/new/"
HIGH = {"function": "7", "physical": "3", "maintenance": "3", "incidents": "1"}  # 14
LIFE = {"function": "10", "physical": "5", "maintenance": "3", "incidents": "0"}  # 18


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def pm_only(client, tenant):
    """A custom role with PM View and no Equipment access at all."""
    with tenant_context(tenant):
        role = Role.objects.create(name="PM viewer", slug="pm-viewer")
        role.set_levels({"pm": Level.VIEW, "equipment": Level.NONE})
    user = User.objects.create_user(username="pmonly@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
    client.force_login(user)
    return user


@pytest.fixture
def theirs(tenant, other_tenant):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        return DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")


def url(dm, part=""):
    return f"/pm/models/{dm.pk}/" + (f"{part}/" if part else "")


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def field_error(body: str, field: str, prefix="dm") -> str:
    marker = f'id="{prefix}-{field}_error">'
    return body.split(marker, 1)[1].split("</span>", 1)[0] if marker in body else ""


def model_post(**over) -> dict:
    return {"manufacturer": "Mindray", "model": "BeneVision N12", "description": "Patient monitor", "category": "Monitors", "risk_class": RiskClass.HIGH,
            "oem_pm_interval_months": "12", "expected_life_years": "7", "list_cost": "9500", **over}


def edit_post(dm, **over) -> dict:
    return {"manufacturer": dm.manufacturer, "model": dm.model, "description": dm.description, "category": dm.category,
            "oem_pm_interval_months": str(dm.oem_pm_interval_months), "expected_life_years": str(dm.expected_life_years), "list_cost": str(dm.list_cost),
            **over}


def assert_saved(r, toast: str):
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer", r.content.decode()[:500]
    t = triggers(r)
    assert t["toast"] == {"value": toast} and "models-changed" in t
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")


# --- the PM program tab --------------------------------------------------------------------------------------------------

def test_program_tab_details_for_an_unscored_model_on_aem(client, signed_in, pump, dept, pump_model):
    signed_in("director")
    Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, next_pm_on=TODAY + timedelta(days=3))
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    r = client.get(url(pump_model), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>BD Alaris 8015 PCU</h2>" in body and "Infusion pump · Infusion pumps" in body
    assert 'class="tab active" role="tab" aria-selected="true" hx-get="' + url(pump_model) + '?tab=program"' in body
    assert "<dt>Risk class</dt><dd>High <span class=\"muted\">· not scored</span></dd>" in body
    assert "<dt>Risk reviewed</dt><dd>Never <span class=\"chip warn\">Review due</span></dd>" in body
    assert "<dt>OEM interval</dt><dd>12 months</dd>" in body and "<dt>Interval in force</dt><dd>18 months, AEM" in body
    assert f'hx-get="{url(pump_model)}?tab=aem" hx-target="#drawer"' in body and "AEM tab</button>" in body
    assert "On an approved AEM interval of 18 months instead of the OEM's 12 months." in body
    assert '<dt>PM procedure</dt><dd><span class="muted">None</span>' in body and f'hx-get="{url(pump_model)}?tab=procedure"' in body
    assert "<dt>List cost</dt><dd>$3,200</dd>" in body and "<dt>Expected life</dt><dd>8 yr</dd>" in body
    assert "Not scored: the high class was set by hand." in body
    # devices: active ones, soonest PM first, opening the device drawer; the retired one only counted
    assert "2 active · 1 retired" in body
    assert body.index('hx-get="/equipment/CE-10003/"') < body.index('hx-get="/equipment/CE-10002/"') and "CE-10004" not in body
    assert '<a class="tag" href="/equipment/CE-10003/">CE-10003</a> · ICU' in body
    # the director may edit and score
    assert f'hx-get="{url(pump_model, "edit")}" hx-target="#modal-card">Edit details</button>' in body
    assert f'hx-get="{url(pump_model, "risk")}" hx-target="#modal-card">Score risk</button>' in body


def test_program_tab_for_a_scored_life_support_model_with_a_procedure(client, signed_in, vent, vent_model):
    signed_in("director")
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="Hamilton-G5 6-month PM")
    vent_model.aem_interval_months = 12  # on file, but life support ignores it
    vent_model.save()
    eq.set_risk_score(vent_model, **{k: int(v) for k, v in LIFE.items()}, today=TODAY - timedelta(days=30))
    body = client.get(url(vent_model), **HX).content.decode()
    assert "<dt>Risk score</dt><dd>18 · Life support</dd>" in body and '<span class="chip neutral" title="Risk score: 18">Score 18</span>' in body
    reviewed = TODAY - timedelta(days=30)
    assert f"<dt>Risk reviewed</dt><dd>{reviewed:%b} {reviewed.day}, {reviewed.year}</dd>" in body and "Review due" not in body
    assert "<dt>Interval in force</dt><dd>6 months, OEM" in body and "An AEM interval is on file, but life-support devices never go on AEM" in body
    assert "<dt>PM procedure</dt><dd>HA-G5-PM6 " in body
    for label, value, meaning in [("Clinical function", 10, "Life support"), ("Physical risk of failure", 5, "Failure could cause death"),
                                  ("Maintenance requirement", 3, "Average: performance and safety checks"), ("Incident history", 0, "None of note")]:
        assert f'<div class="t">{label}</div><div class="s">{meaning}</div></div><div class="r"><b>{value}</b>' in body
    assert "Total 18: life support." in body and ">Risk score</button>" in body


def test_program_tab_counts_devices_beyond_the_short_list(client, signed_in, dept, pump_model):
    signed_in("technician")
    for n in range(11):
        Asset.objects.create(tag=f"CE-3{n:04d}", device_model=pump_model, department=dept, next_pm_on=TODAY + timedelta(days=n))
    body = client.get(url(pump_model), **HX).content.decode()
    assert "11 active" in body and body.count('hx-get="/equipment/CE-3') == 8 and "CE-30008" not in body
    assert "And 3 more active devices" in body and 'href="/equipment/?q=Alaris%208015%20PCU&amp;status=active"' in body


def test_program_tab_without_devices(client, signed_in, pump_model):
    signed_in("analyst")
    assert "No devices of this model in use." in client.get(url(pump_model), **HX).content.decode()


@pytest.mark.parametrize("slug, edit, risk", [("director", True, True), ("manager", True, False), ("technician", True, False), ("analyst", False, False)])
def test_program_tab_buttons_follow_the_equipment_levels(client, signed_in, pump_model, slug, edit, risk):
    signed_in(slug)
    body = client.get(url(pump_model), **HX).content.decode()
    assert (url(pump_model, "edit") in body) is edit and (url(pump_model, "risk") in body) is risk
    assert ('class="sec-actions"' in body) is (edit or risk)


def test_device_rows_open_the_device_drawer_only_with_equipment_view(client, pm_only, pump):
    body = client.get(url(pump.device_model), **HX).content.decode()
    assert '<div class="li static">' in body and "CE-10002 · ICU" in body
    assert "/equipment/CE-10002/" not in body and "/equipment/?q=" not in body
    assert "Edit details" not in body and url(pump.device_model, "risk") not in body


def test_the_tabs_render_from_the_model_url(client, signed_in, pump_model):
    signed_in("analyst")
    for tab in ("procedure", "aem"):
        body = client.get(f"{url(pump_model)}?tab={tab}", **HX).content.decode()
        assert f'class="tab active" role="tab" aria-selected="true" hx-get="{url(pump_model)}?tab={tab}"' in body


def test_a_full_page_load_renders_the_pm_schedule_with_the_drawer_open(client, signed_in, pump, pump_model):
    signed_in("technician")
    r = client.get(url(pump_model))
    body = r.content.decode()
    assert r.status_code == 200 and "<html" in body and 'id="pm-body"' in body and 'id="pm-panels"' in body
    assert 'class="drawer open" id="drawer" aria-hidden="false"' in body and "<h2>BD Alaris 8015 PCU</h2>" in body
    assert "<dt>Interval in force</dt><dd>18 months, AEM" in body


def test_the_program_tab_keeps_its_context_under_one_key(client, signed_in, pump, pump_model):
    """A drawer opened directly renders over the PM schedule; its tab must not shadow the schedule's keys (devices, can_view_asset)."""
    from apps.web.views_models import program_tab

    signed_in("director")
    r = client.get(url(pump_model))
    assert set(program_tab(r.wsgi_request, pump_model)) == {"program"}
    assert "day_rows" in r.context and r.context["program"]["devices"]["active"] == 1


def test_the_model_drawer_needs_pm_view(client, signed_in, pump_model):
    signed_in("requester")  # PM None
    assert client.get(url(pump_model), **HX).status_code == 403


def test_another_facilitys_model_is_a_404_everywhere(client, signed_in, pump_model, theirs):
    signed_in("director")
    for part in ("", "edit", "risk"):
        assert client.get(url(theirs, part), **HX).status_code == 404, part
    assert client.post(url(theirs, "edit"), edit_post(theirs, description="Mine now"), **HX).status_code == 404
    assert client.post(url(theirs, "risk"), HIGH, **HX).status_code == 404
    assert client.post(url(theirs, "risk"), {"clear": "1"}, **HX).status_code == 404
    with tenant_context(theirs.tenant):
        theirs.refresh_from_db()
    assert theirs.description == "Their pump" and theirs.risk_score is None


# --- Add model -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug, status", [("director", 200), ("manager", 200), ("technician", 200), ("analyst", 403), ("requester", 403), ("vendor", 403)])
def test_add_model_needs_equipment_edit_on_get_and_post(client, signed_in, slug, status, ctx):
    signed_in(slug)
    assert client.get(NEW_URL, **HX).status_code == status
    r = client.post(NEW_URL, model_post(), **HX)
    assert r.status_code == status
    assert DeviceModel.objects.filter(model="BeneVision N12").exists() is (status == 200)


def test_add_model_needs_pm_view_even_with_equipment_edit(client, tenant):
    with tenant_context(tenant):
        role = Role.objects.create(name="Equipment only", slug="eq-only")
        role.set_levels({"equipment": Level.FULL, "pm": Level.NONE})
    client.force_login(User.objects.create_user(username="eq@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    assert client.get(NEW_URL, **HX).status_code == 403 and client.post(NEW_URL, model_post(), **HX).status_code == 403


def test_add_model_modal(client, signed_in, pump_model, vent_model):
    signed_in("technician")
    body = client.get(NEW_URL, **HX).content.decode()
    assert "<h2>Add model</h2>" in body and f'hx-post="{NEW_URL}" hx-target="#modal-card" novalidate' in body
    assert '<option value="Infusion pumps"></option>' in body and '<option value="Ventilators"></option>' in body and 'list="dm-categories"' in body
    assert '<option value="medium" selected>Medium</option>' in body and "Scoring the model with the rubric sets it from then on." in body
    for name in ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "expected_life_years", "list_cost"):
        assert f'name="{name}"' in body
    assert "aem_interval_months" not in body and ">Add model</button>" in body


def test_add_model_creates_it_and_opens_its_drawer(client, signed_in, ctx):
    signed_in("technician")
    r = client.post(NEW_URL, model_post(manufacturer="  Mindray ", list_cost=""), **HX)
    assert_saved(r, "Mindray BeneVision N12 added to the catalog")
    dm = DeviceModel.objects.get(model="BeneVision N12")
    assert (dm.manufacturer, dm.description, dm.category, dm.risk_class, dm.oem_pm_interval_months, dm.expected_life_years, dm.list_cost) == (
        "Mindray", "Patient monitor", "Monitors", RiskClass.HIGH, 12, 7, Decimal("0"))
    assert dm.risk_score is None and dm.aem_interval_months is None
    body = r.content.decode()
    assert "<h2>Mindray BeneVision N12</h2>" in body and 'aria-selected="true" hx-get="' + url(dm) + '?tab=program"' in body


@pytest.mark.parametrize("over, field, message", [
    ({"manufacturer": ""}, "manufacturer", "This is required."),
    ({"category": "   "}, "category", "This is required."),
    ({"manufacturer": "bd", "model": "ALARIS 8015 PCU"}, "model", "is already in the catalog; choose it from the list."),
    ({"oem_pm_interval_months": "0"}, "oem_pm_interval_months", "The PM interval is 1 to 120 months."),
    ({"oem_pm_interval_months": ""}, "oem_pm_interval_months", "The PM interval is 1 to 120 months."),
    ({"expected_life_years": "51"}, "expected_life_years", "Expected life is 1 to 50 years."),
    ({"list_cost": "-1"}, "list_cost", "The list cost is a number, 0 or more."),
    ({"risk_class": ""}, "risk_class", "Choose a risk class."),
    ({"risk_class": "extreme"}, "risk_class", "Select a valid choice."),
])
def test_add_model_errors_land_on_their_fields(client, signed_in, pump_model, over, field, message):
    signed_in("technician")
    r = client.post(NEW_URL, model_post(**over), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "HX-Trigger" not in r and "<h2>Add model</h2>" in body
    assert message in field_error(body, field), body
    assert f'name="{field}"' in body.split(f'id="dm-{field}_error"')[0].rsplit('<div class="fld', 1)[1] and "autofocus" in body
    assert DeviceModel.objects.count() == 1


# --- Edit details ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug, status", [("director", 200), ("manager", 200), ("technician", 200), ("analyst", 403), ("requester", 403)])
def test_edit_details_needs_equipment_edit_on_get_and_post(client, signed_in, pump_model, slug, status):
    signed_in(slug)
    assert client.get(url(pump_model, "edit"), **HX).status_code == status
    assert client.post(url(pump_model, "edit"), edit_post(pump_model, description="Large-volume pump"), **HX).status_code == status
    pump_model.refresh_from_db()
    assert (pump_model.description == "Large-volume pump") is (status == 200)


def test_edit_details_modal(client, signed_in, pump_model):
    signed_in("technician")
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in HIGH.items()})
    body = client.get(url(pump_model, "edit"), **HX).content.decode()
    assert "<h2>Edit BD Alaris 8015 PCU</h2>" in body and f'hx-post="{url(pump_model, "edit")}"' in body
    assert 'name="manufacturer" value="BD"' in body and 'name="oem_pm_interval_months" value="12"' in body and 'name="list_cost" value="3200.00"' in body
    assert 'name="risk_class"' not in body and 'value="14 · High" readonly' in body and "Set by the risk score (Equipment Approve), not here." in body
    assert "A new interval applies from each device&#x27;s next PM; no date moves now." in body
    assert "An AEM interval is changed on the AEM tab, and the PM procedure on the Procedure tab." in body


def test_edit_details_saves_through_the_service(client, signed_in, pump, pump_model, monkeypatch):
    heard = []
    monkeypatch.setattr("apps.pm.aem.model_changed", lambda dm, changed, by=None: heard.append(sorted(changed)))
    signed_in("technician")
    next_pm = pump.next_pm_on
    r = client.post(url(pump_model, "edit"), edit_post(pump_model, description=" Large-volume  pump ", oem_pm_interval_months="24", list_cost="3300.50",
                                                       risk_class=RiskClass.LOW, aem_interval_months="6"), **HX)
    assert_saved(r, "BD Alaris 8015 PCU updated")
    pump_model.refresh_from_db()
    assert (pump_model.description, pump_model.oem_pm_interval_months, pump_model.list_cost) == ("Large-volume pump", 24, Decimal("3300.50"))
    assert pump_model.risk_class == RiskClass.HIGH and pump_model.aem_interval_months == 18  # neither is on this form
    assert heard == [["description", "list_cost", "oem_pm_interval_months"]]
    pump.refresh_from_db()
    assert pump.next_pm_on == next_pm  # a new interval applies from the next PM
    assert "<dt>OEM interval</dt><dd>24 months</dd>" in r.content.decode()


@pytest.mark.parametrize("over, field, message", [
    ({"model": ""}, "model", "This is required."),
    ({"manufacturer": "HAMILTON MEDICAL", "model": "hamilton-g5"}, "model", "is already in the catalog"),
    ({"oem_pm_interval_months": "121"}, "oem_pm_interval_months", "The PM interval is 1 to 120 months."),
    ({"oem_pm_interval_months": "6.5"}, "oem_pm_interval_months", "Enter a whole number."),
])
def test_edit_details_errors_land_on_their_fields(client, signed_in, pump_model, vent_model, over, field, message):
    signed_in("technician")
    r = client.post(url(pump_model, "edit"), edit_post(pump_model, **over), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "<h2>Edit BD Alaris 8015 PCU</h2>" in body
    assert message in field_error(body, field), body
    pump_model.refresh_from_db()
    assert (pump_model.manufacturer, pump_model.model, pump_model.oem_pm_interval_months) == ("BD", "Alaris 8015 PCU", 12)


# --- the risk score --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["manager", "technician", "analyst", "requester", "vendor"])
def test_the_risk_score_needs_equipment_approve_on_get_and_post(client, signed_in, pump_model, slug):
    signed_in(slug)
    risk = url(pump_model, "risk")
    assert client.get(risk, **HX).status_code == 403
    assert client.get(risk, LIFE, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="rk-total").status_code == 403
    assert client.post(risk, LIFE, **HX).status_code == 403
    assert client.post(risk, {"clear": "1"}, **HX).status_code == 403
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_class == RiskClass.HIGH


def test_a_facility_can_give_its_manager_the_risk_score(client, signed_in, pump_model, tenant):
    with tenant_context(tenant):
        Role.objects.get(slug="manager").set_levels({"equipment": Level.APPROVE})
    signed_in("manager")
    assert client.get(url(pump_model, "risk"), **HX).status_code == 200


def test_risk_modal_for_an_unscored_model(client, signed_in, pump_model):
    signed_in("director")
    body = client.get(url(pump_model, "risk"), **HX).content.decode()
    assert "<h2>Risk score · BD Alaris 8015 PCU</h2>" in body and "Not scored yet: the high class was set by hand." in body
    assert "Score = clinical function (1 to 10) + physical risk of failure (1 to 5)" in body
    assert '<label for="rk-function">Clinical function, 1 to 10</label>' in body and '<label for="rk-incidents">Incident history, 0 to 2</label>' in body
    assert '<option value="10">10 · Life support</option>' in body and '<option value="1">1 · No patient contact</option>' in body
    assert '<option value="5">5 · Failure could cause death</option>' in body and '<option value="0">0 · None of note</option>' in body
    assert body.index('<option value="10">') < body.index('<option value="1">')  # highest first
    assert "Choose all four parts to see the total and its class." in body and "Clear score" not in body
    assert "Bands: 16 and above life support · 12 to 15 high · 9 to 11 medium · 8 and below low." in body
    # the selects ask for the running total, with every choice in the form
    assert f'hx-get="{url(pump_model, "risk")}" hx-trigger="change" hx-include="#rk-form" hx-target="#rk-total"' in body


def test_risk_modal_for_a_scored_model_starts_from_its_score(client, signed_in, pump_model):
    signed_in("director")
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in HIGH.items()}, today=date(2024, 5, 1))
    body = client.get(url(pump_model, "risk"), **HX).content.decode()
    assert '<option value="7" selected>7 · Surgical and intensive care monitoring</option>' in body
    assert '<option value="1" selected>1 · Some incidents or recalls</option>' in body
    assert "Scored 14 (High), reviewed May 1, 2024: the yearly review is due." in body
    assert '<div class="v">14</div><div class="l">total score</div>' in body and ">Clear score</button>" in body


def test_the_running_total(client, signed_in, pump_model):
    signed_in("director")
    r = client.get(url(pump_model, "risk"), LIFE, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="rk-total")
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>" not in body and "<form" not in body
    assert '<div class="v">18</div>' in body and '<span class="chip crit">Life support</span>' in body and "new risk class, was High" in body
    body = client.get(url(pump_model, "risk"), HIGH, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="rk-total").content.decode()
    assert '<div class="v">14</div>' in body and "risk class, unchanged" in body and "was High" not in body
    for partial in ({**HIGH, "incidents": ""}, {**HIGH, "function": "11"}, {**HIGH, "physical": "x"}, {**HIGH, "maintenance": "²"}):
        body = client.get(url(pump_model, "risk"), partial, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="rk-total").content.decode()
        assert "Choose all four parts" in body
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None  # a preview saves nothing


def test_scoring_into_another_class_says_so(client, signed_in, pump_model):
    signed_in("director")
    r = client.post(url(pump_model, "risk"), LIFE, **HX)
    assert_saved(r, "BD Alaris 8015 PCU scored 18: risk class now Life support")
    pump_model.refresh_from_db()
    assert pump_model.risk_score == 18 and pump_model.risk_class == RiskClass.LIFE_SUPPORT and pump_model.risk_reviewed_on == TODAY
    assert pump_model.pm_interval_months == 12  # life support: off AEM
    body = r.content.decode()
    assert "<dt>Risk score</dt><dd>18 · Life support</dd>" in body and '<span class="chip crit">Life support</span>' in body


def test_scoring_in_the_same_class_and_the_yearly_review(client, signed_in, pump_model):
    signed_in("director")
    assert_saved(client.post(url(pump_model, "risk"), HIGH, **HX), "BD Alaris 8015 PCU scored 14 · High")
    DeviceModel.objects.filter(pk=pump_model.pk).update(risk_reviewed_on=date(2024, 1, 1))
    assert_saved(client.post(url(pump_model, "risk"), HIGH, **HX), "BD Alaris 8015 PCU risk review recorded: 14 · High")
    pump_model.refresh_from_db()
    assert pump_model.risk_reviewed_on == TODAY


def test_a_missing_or_bad_part_comes_back_with_its_error(client, signed_in, pump_model):
    signed_in("director")
    r = client.post(url(pump_model, "risk"), {**HIGH, "maintenance": ""}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "<h2>Risk score · BD Alaris 8015 PCU</h2>" in body
    assert field_error(body, "maintenance", "rk") == "Maintenance requirement is a whole number from 1 to 5."
    assert '<option value="7" selected>' in body  # the other choices come back as made
    r = client.post(url(pump_model, "risk"), {**HIGH, "function": "12"}, **HX)
    assert "Select a valid choice." in field_error(r.content.decode(), "function", "rk")
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_class == RiskClass.HIGH


def test_clearing_the_score(client, signed_in, pump_model):
    signed_in("director")
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in LIFE.items()})
    r = client.post(url(pump_model, "risk"), {"clear": "1", **HIGH}, **HX)
    assert_saved(r, "Risk score cleared for BD Alaris 8015 PCU; its class stays Life support")
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_reviewed_on is None and pump_model.risk_class == RiskClass.LIFE_SUPPORT
    r = client.post(url(pump_model, "risk"), {"clear": "1"}, **HX)  # nothing left to clear
    assert r.status_code == 200 and "HX-Retarget" not in r and triggers(r)["toast"] == {"value": "BD Alaris 8015 PCU has no risk score to clear"}


# --- the device drawer, Settings, the API --------------------------------------------------------------------------------

def test_the_device_drawer_shows_the_score_when_scored(client, signed_in, pump, pump_model):
    signed_in("technician")
    assert "<dt>Risk class</dt><dd>High</dd>" in client.get(f"/equipment/{pump.tag}/", **HX).content.decode()
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in HIGH.items()})
    assert "<dt>Risk score</dt><dd>14 · High</dd>" in client.get(f"/equipment/{pump.tag}/", **HX).content.decode()


def test_settings_says_how_many_models_are_scored(client, signed_in, pump_model, vent_model):
    signed_in("director")
    assert "0 of 2 models scored with the rubric. Models are scored" in client.get("/settings/").content.decode()
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in HIGH.items()}, today=TODAY - timedelta(days=400))
    assert "1 of 2 models scored with the rubric · 1 yearly review due." in client.get("/settings/").content.decode()


def api_patch(client, dm, body):
    return client.patch(f"/api/v1/device-models/{dm.pk}/", body, content_type="application/json")


def test_api_a_technician_cannot_change_the_risk_class(client, signed_in, pump_model):
    signed_in("technician")
    assert api_patch(client, pump_model, {"risk_class": "low"}).status_code == 403
    assert api_patch(client, pump_model, {"risk_class": "high", "description": "Large-volume pump"}).status_code == 200  # its own class is fine
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.HIGH and pump_model.description == "Large-volume pump"


def test_api_the_director_changes_an_unscored_class_but_not_against_a_score(client, signed_in, pump_model):
    signed_in("director")
    r = api_patch(client, pump_model, {"risk_class": "medium"})
    assert r.status_code == 200 and r.json()["risk_class"] == "medium"
    eq.set_risk_score(pump_model, **{k: int(v) for k, v in HIGH.items()})  # 14: high again
    r = api_patch(client, pump_model, {"risk_class": "low"})
    assert r.status_code == 400 and "follows its risk score (14, High)" in r.json()["risk_class"][0]
    assert api_patch(client, pump_model, {"risk_class": "high"}).status_code == 200
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.HIGH


def test_api_cannot_set_the_score_parts(client, signed_in, pump_model):
    signed_in("director")
    api_patch(client, pump_model, {"risk_function": 10, "risk_physical": 5, "risk_maintenance": 5, "risk_incidents": 2, "risk_reviewed_on": "2026-01-01"})
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_reviewed_on is None

