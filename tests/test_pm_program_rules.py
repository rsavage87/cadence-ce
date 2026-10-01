"""Rules settled in slice 14's review: who may end an AEM (only End AEM, never an OEM edit), an interval on file without approval on
a model leaving life support, odd numbers never reaching int(), approval after the OEM interval changed, ending an unused interval
on a life-support model, what scoring into life support says and refreshes, the API's procedure door, and month wording."""
import json
from datetime import date, timedelta

import pytest

from apps.accounts.models import Level, Role, User
from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus, PmProcedure

TODAY = date.today()
HX = {"HTTP_HX_REQUEST": "true"}


def years_ago(n: int) -> date:
    return add_months(TODAY, -12 * n)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def monitor(ctx, dept):
    """High risk, OEM 12, one device installed four years ago (the policy's history)."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
    Asset.objects.create(tag="M-1", device_model=dm, department=dept, installed_on=years_ago(4), last_pm_on=add_months(TODAY, -14),
                         next_pm_on=add_months(TODAY, 10))
    return dm


@pytest.fixture
def people(make_user):
    return {"tech": make_user("technician"), "manager": make_user("manager"), "director": make_user("director")}


def in_force(dm, people, months=24):
    d = aem.propose(dm, interval_months=months, rationale="Few failures.", by=people["tech"])
    aem.approve(d, by=people["manager"], decided_on=TODAY, note="EMC minutes")
    dm.refresh_from_db()
    return d


def custom_user(tenant, levels, username="custom@riverside.example"):
    role = Role.objects.create(name="Custom", slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def triggers(r) -> dict:
    return json.loads(r["HX-Trigger"]) if "HX-Trigger" in r else {}


# --- an OEM edit never ends a committee decision ------------------------------------------------------------------------------

def test_a_technician_cannot_end_an_aem_by_editing_the_oem_interval(client, monitor, people):
    d = in_force(monitor, people)
    client.force_login(people["tech"])
    assert client.get(f"/pm/aem/{d.pk}/end/", **HX).status_code == 403
    post = {"manufacturer": monitor.manufacturer, "model": monitor.model, "description": monitor.description, "category": monitor.category,
            "oem_pm_interval_months": "24", "expected_life_years": "8", "list_cost": "0"}
    r = client.post(f"/pm/models/{monitor.pk}/edit/", post, **HX)
    assert r.status_code == 200 and "End the AEM on the model&#x27;s AEM tab first" in r.content.decode()
    d.refresh_from_db()
    monitor.refresh_from_db()
    assert d.status == AemStatus.APPROVED and monitor.oem_pm_interval_months == 12 and monitor.pm_interval_months == 24


def test_the_api_refuses_the_same_oem_change(client, monitor, people):
    in_force(monitor, people)
    client.force_login(people["tech"])
    r = client.patch(f"/api/v1/device-models/{monitor.pk}/", {"oem_pm_interval_months": 24}, content_type="application/json")
    assert r.status_code == 400 and "End the AEM" in r.json()["oem_pm_interval_months"][0]


# --- leaving life support -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["class", "score"])
def test_an_unapproved_interval_on_a_life_support_model_is_dropped_when_it_leaves_life_support(ctx, dept, vent_model, people, how):
    """It never applied there (life support follows the OEM interval) and must not start applying without the committee."""
    DeviceModel.objects.filter(pk=vent_model.pk).update(aem_interval_months=24)  # set before approvals were recorded (admin, old API)
    vent_model.refresh_from_db()
    if how == "class":
        eq.update_device_model(vent_model, risk_class=RiskClass.HIGH, by=people["director"])
    else:
        eq.set_risk_score(vent_model, function=7, physical=4, maintenance=2, incidents=0, by=people["director"])  # 13: high
    vent_model.refresh_from_db()
    assert vent_model.risk_class == RiskClass.HIGH and vent_model.aem_interval_months is None
    assert vent_model.pm_interval_months == vent_model.oem_pm_interval_months and not AemDecision.objects.exists()
    assert vent_model.history.first().history_change_reason.startswith("AEM on file without a recorded approval cleared")


def test_an_approved_interval_is_untouched_by_other_class_changes(monitor, people):
    d = in_force(monitor, people)
    eq.update_device_model(monitor, risk_class=RiskClass.MEDIUM, by=people["director"])
    d.refresh_from_db()
    monitor.refresh_from_db()
    assert d.status == AemStatus.APPROVED and monitor.aem_interval_months == 24


# --- odd numbers --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["2²", "²", "9" * 5000, "١٢"])
def test_odd_digits_are_a_field_error_never_a_500(client, monitor, people, value):
    with pytest.raises(Exception) as e:
        aem.propose(monitor, interval_months=value, rationale="x", by=people["tech"])
    assert "interval_months" in e.value.message_dict
    client.force_login(people["tech"])
    r = client.post(f"/pm/models/{monitor.pk}/aem/propose/", {"interval_months": value, "rationale": "Few failures."}, **HX)
    assert r.status_code == 200 and "whole number of months" in r.content.decode()


def test_the_risk_total_ignores_a_huge_number(client, monitor, people):
    client.force_login(people["director"])
    r = client.get(f"/pm/models/{monitor.pk}/risk/?function={'9' * 5000}&physical=5&maintenance=5&incidents=2",
                   HTTP_HX_REQUEST="true", HTTP_HX_TARGET="rk-total")
    assert r.status_code == 200 and "total score" not in r.content.decode()


# --- approval after the OEM interval changed ------------------------------------------------------------------------------------

def test_a_proposal_whose_oem_interval_changed_is_proposed_again(client, monitor, people):
    d = aem.propose(monitor, interval_months=24, rationale="An extension of the 12-month OEM interval.", by=people["tech"])
    eq.update_device_model(monitor, oem_pm_interval_months=36)  # now 24 would be a shortening
    with pytest.raises(Exception) as e:
        aem.approve(d, by=people["manager"], decided_on=TODAY, note="EMC")
    assert "The OEM interval changed since this proposal (from 12 months to 36 months)" in " ".join(e.value.messages)
    client.force_login(people["manager"])
    body = client.get(f"/pm/aem/{d.pk}/decide/", **HX).content.decode()
    assert "The OEM interval changed since this proposal (from 12 months to 36 months). Withdraw it and propose again" in body
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


# --- an unused interval on a life-support model ----------------------------------------------------------------------------------

def test_ending_an_unused_interval_on_a_life_support_model_moves_nothing(client, ctx, dept, vent_model, people):
    DeviceModel.objects.filter(pk=vent_model.pk).update(aem_interval_months=12)
    vent = Asset.objects.create(tag="V-1", device_model=vent_model, department=dept, installed_on=TODAY - timedelta(days=800),
                                next_pm_on=TODAY + timedelta(days=10))  # no PM on record: the pull-in would have brought it to today
    client.force_login(people["director"])
    body = client.get(f"/pm/aem/{vent_model.pk}/end/", **HX).content.decode()
    assert "clear the unused AEM interval of 12 months (life support follows the OEM's 6)" in body
    assert "No device's next PM moves: this model's devices have always followed the OEM interval." in body
    r = client.post(f"/pm/aem/{vent_model.pk}/end/", {"reason": "Clearing an old value."}, **HX)
    assert r.status_code == 200 and "devices-changed" not in triggers(r)
    vent.refresh_from_db()
    vent_model.refresh_from_db()
    assert vent.next_pm_on == TODAY + timedelta(days=10) and vent_model.aem_interval_months is None


# --- scoring into life support says what happened --------------------------------------------------------------------------------

def test_scoring_into_life_support_says_the_aem_ended_and_refreshes_the_lists(client, monitor, people, dept):
    in_force(monitor, people)
    far = Asset.objects.create(tag="M-2", device_model=monitor, department=dept, installed_on=years_ago(4),
                               last_pm_on=add_months(TODAY, -14), next_pm_on=add_months(TODAY, 10))
    client.force_login(people["director"])
    r = client.post(f"/pm/models/{monitor.pk}/risk/", {"function": "10", "physical": "5", "maintenance": "3", "incidents": "0"}, **HX)
    t = triggers(r)
    assert r.status_code == 200 and {"models-changed", "devices-changed", "wo-changed"} <= set(t)
    assert t["toast"]["value"] == ("Philips IntelliVue MX750 scored 18: risk class now Life support; its AEM interval ended (life support "
                                   "follows the OEM interval); 2 devices' next PM moved earlier")
    far.refresh_from_db()
    assert far.next_pm_on == TODAY


def test_a_score_that_changes_nothing_in_aem_refreshes_only_the_library(client, monitor, people):
    client.force_login(people["director"])
    r = client.post(f"/pm/models/{monitor.pk}/risk/", {"function": "7", "physical": "4", "maintenance": "2", "incidents": "0"}, **HX)
    t = triggers(r)
    assert "models-changed" in t and "devices-changed" not in t and t["toast"]["value"] == "Philips IntelliVue MX750 scored 13 · High"


# --- the API's procedure door --------------------------------------------------------------------------------------------------

@pytest.fixture
def procedure(ctx):
    return PmProcedure.objects.create(code="PH-MX750-PM12", name="Patient monitor PM", estimated_hours=1, checklist=["Inspect"])


def test_the_api_needs_pm_edit_to_choose_a_procedure(client, tenant, monitor, procedure):
    client.force_login(custom_user(tenant, {"equipment": Level.EDIT, "pm": Level.VIEW}))
    r = client.patch(f"/api/v1/device-models/{monitor.pk}/", {"pm_procedure": str(procedure.pk)}, content_type="application/json")
    assert r.status_code == 403
    monitor.refresh_from_db()
    assert monitor.pm_procedure_id is None
    # Other fields stay at Equipment Edit; sending back the current procedure is fine.
    r = client.patch(f"/api/v1/device-models/{monitor.pk}/", {"description": "Bedside monitor", "pm_procedure": None}, content_type="application/json")
    assert r.status_code == 200


def test_the_api_records_a_chosen_procedure_like_the_screen(client, monitor, procedure, people):
    client.force_login(people["tech"])  # PM Edit
    r = client.patch(f"/api/v1/device-models/{monitor.pk}/", {"pm_procedure": str(procedure.pk)}, content_type="application/json")
    assert r.status_code == 200
    monitor.refresh_from_db()
    h = monitor.history.first()
    assert monitor.pm_procedure == procedure and h.history_change_reason == "PM procedure: PH-MX750-PM12" and h.history_user == people["tech"]


def test_a_refused_api_change_leaves_the_procedure_as_it_was(client, monitor, procedure, people):
    client.force_login(people["tech"])
    r = client.patch(f"/api/v1/device-models/{monitor.pk}/", {"pm_procedure": str(procedure.pk), "list_cost": "-5"}, content_type="application/json")
    assert r.status_code == 400
    monitor.refresh_from_db()
    assert monitor.pm_procedure_id is None  # all or nothing


def test_the_api_says_where_an_aem_interval_comes_from(client, ctx, people):
    client.force_login(people["director"])
    r = client.post("/api/v1/device-models/", {"manufacturer": "GE", "model": "B40", "description": "Monitor", "category": "Monitors",
                                               "risk_class": "medium", "aem_interval_months": 24}, content_type="application/json")
    assert r.status_code == 400 and r.json()["aem_interval_months"] == ["An AEM interval is set by approving an AEM proposal (PM schedule, PM library)."]


# --- month wording -------------------------------------------------------------------------------------------------------------

def test_a_one_month_interval_reads_one_month(client, ctx, dept, people):
    dm = DeviceModel.objects.create(manufacturer="Steris", model="V-PRO", description="Sterilizer", category="Sterilization",
                                    risk_class=RiskClass.MEDIUM, oem_pm_interval_months=3)
    Asset.objects.create(tag="S-1", device_model=dm, department=dept, installed_on=years_ago(5), next_pm_on=TODAY + timedelta(days=20))
    in_force(dm, people, months=1)
    client.force_login(people["manager"])
    body = client.get(f"/pm/models/{dm.pk}/?tab=aem", **HX).content.decode()
    assert "every 1 month instead of the OEM" in body and "1 months" not in body
