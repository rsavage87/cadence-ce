"""Slice 15 through the API: completing through transition takes the resolution and a PM's results (as the drawer's Mark
completed), the recorded results read back read-only, and the Settings API reads and changes the labor rates."""
from decimal import Decimal

import pytest

from apps.pm.models import PmProcedure
from apps.workorders.models import PmResult, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def pm(ctx, vent, techs):
    proc = PmProcedure.objects.create(code="HA-G5-PM6", name="Ventilator PM", estimated_hours=Decimal("1.5"),
                                      checklist=["Visual inspection", {"text": "Leakage current", "measure": "µA"}])
    vent.device_model.pm_procedure = proc
    vent.device_model.save()
    wo = create_work_order(asset=vent, type=WoType.PM, priority="high", problem="Scheduled 6-month PM")
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def post(client, wo, body):
    return client.post(f"/api/v1/work-orders/{wo.id}/transition/", body, content_type="application/json")


def test_a_pm_completes_through_the_api_with_its_results(client, signed_in, pm):
    signed_in("technician")
    r = post(client, pm, {"status": "completed", "pm_result": "pass"})
    assert r.status_code == 400 and {"step_1", "step_2"} & set(r.json())  # every step needs its result
    r = post(client, pm, {"status": "completed", "pm_result": "pass",
                          "results": [{"result": "pass"}, {"result": "pass", "reading": "42"}]})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "completed" and body["pm_result"] == PmResult.PASS
    assert [(s["text"], s["result"], s["reading"]) for s in body["checklist_results"]] == [("Visual inspection", "pass", ""), ("Leakage current", "pass", "42")]


def test_recorded_results_are_read_only_through_patch(client, signed_in, pm):
    signed_in("director")
    r = client.patch(f"/api/v1/work-orders/{pm.id}/", {"pm_result": "fail", "checklist_results": []}, content_type="application/json")
    pm.refresh_from_db()
    assert r.status_code == 200 and pm.pm_result == "" and pm.checklist_results == []


def test_the_settings_api_reads_and_changes_the_labor_rates(client, signed_in, ctx):
    signed_in("director")
    assert {k: client.get("/api/v1/settings/").json()[k] for k in ("labor_rate", "vendor_labor_rate")} == {"labor_rate": "82.00", "vendor_labor_rate": "215.00"}
    r = client.patch("/api/v1/settings/", {"labor_rate": "90.50"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["labor_rate"] == "90.50"
    r = client.patch("/api/v1/settings/", {"vendor_labor_rate": "-1"}, content_type="application/json")
    assert r.status_code == 400 and "vendor_labor_rate" in r.json()
