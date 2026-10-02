"""Slice 15 through the API: completing through transition takes the resolution and a PM's results (as the drawer's Mark
completed), the recorded results read back read-only, and the Settings API reads and changes the labor rates."""
from datetime import date
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


# --- slice 15 review: a completed or closed work order is the record ----------------------------------------------------------------

@pytest.fixture
def closed_repair(ctx, vent, pump, techs):
    from apps.workorders import costs
    from apps.workorders.completion import complete_work_order

    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Door latch broken")
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    costs.add_part(wo, description="Latch", quantity=1, unit_cost=Decimal("40"), by=None)
    complete_work_order(wo, resolution="Replaced latch")
    change_status(wo, WoStatus.CLOSED)
    return wo


def test_patch_cannot_rewrite_a_closed_work_order(client, signed_in, closed_repair, pump):
    signed_in("technician")
    url = f"/api/v1/work-orders/{closed_repair.id}/"
    r = client.patch(url, {"resolution": ""}, content_type="application/json")
    assert r.status_code == 400 and "recorded when the work order is completed" in r.json()["resolution"][0]
    r = client.patch(url, {"type": "inspection"}, content_type="application/json")
    assert r.status_code == 400 and "Reopen it first" in r.json()["detail"]
    r = client.patch(url, {"asset": str(pump.id)}, content_type="application/json")
    assert r.status_code == 400
    closed_repair.refresh_from_db()
    assert (closed_repair.type, closed_repair.asset_id, closed_repair.resolution) == (WoType.REPAIR, closed_repair.asset_id, "Replaced latch")


def test_an_open_work_order_with_parts_stays_with_its_device(client, signed_in, ctx, vent, pump, techs):
    from apps.workorders import costs

    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Door latch broken")
    signed_in("technician")
    url = f"/api/v1/work-orders/{wo.id}/"
    assert client.patch(url, {"problem": "Door latch and hinge broken"}, content_type="application/json").status_code == 200  # open: editable
    costs.add_part(wo, description="Latch", quantity=1, unit_cost=Decimal("40"), by=None)
    r = client.patch(url, {"asset": str(pump.id)}, content_type="application/json")
    assert r.status_code == 400 and "stays with that device" in r.json()["asset"][0]


# --- slice 15 review: one rounding rule for a line's cost ---------------------------------------------------------------------------

def test_every_total_rounds_each_line_to_the_cent(client, signed_in, ctx, vent, techs):
    """0.25 h at $82.50 is $20.625: each line is $20.63 wherever lines are added up (drawer, print, CSV, the work order's total)."""
    from apps.workorders import costs
    from apps.workorders.models import LABOR_AMOUNT, LaborLine

    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Alarm")
    for _ in range(2):
        costs.add_labor(wo, hours=Decimal("0.25"), worked_on=date.today(), technician=techs["dana"], rate=Decimal("82.50"), by=None)
    assert wo.labor_cost() == 41.26
    from django.db.models import Sum

    assert LaborLine.objects.filter(work_order=wo).aggregate(s=Sum(LABOR_AMOUNT))["s"] == Decimal("41.26")
    signed_in("director")
    drawer = client.get(f"/work-orders/{wo.number}/", HTTP_HX_REQUEST="true").content.decode()
    assert "$41.26" in drawer
    csv = b"".join(client.get("/export/work-orders.csv?status=").streaming_content).decode("utf-8-sig")
    assert "41.26" in csv


# --- slice 15 review: the feed's last good check is never hidden by a later failure ---------------------------------------------------

def test_a_failed_check_never_hides_the_days_good_one(ctx, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.recalls import feeds

    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise feeds.FeedError("openFDA did not answer in time")
        return feeds.FeedResult(0, 0, 0)

    monkeypatch.setattr(feeds, "import_recalls", fake)
    t0 = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
    feeds.check_feed(now=t0)
    good = feeds.last_checked()
    with pytest.raises(feeds.FeedError):
        feeds.check_feed(now=t0 + timedelta(minutes=20))
    assert feeds.last_checked() == good  # still the 09:00 success
    held = feeds.check_feed(now=t0 + timedelta(minutes=25))
    assert held.result is None and held.last_failed
