"""Cost reports (slice 7): cost of service ratio by category, repair spend trend, contract vs in-house.
Math, tenant isolation, rendering (page and CSV), and the empty tenant, for each report."""
from datetime import date, timedelta

import pytest

from apps.contracts.models import Contract, ContractType
from apps.contracts.services import add_asset
from apps.equipment.models import Asset, Department, DeviceModel, SupportType
from apps.reports.cost import report_contract, report_cosr, report_spend
from apps.reports.services import ANNUALIZE, TRAILING_DAYS, cost_of_service, overview_kpis, run_report
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, PartLine
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 9, 28)
PLAIN = (str, int, float, date, type(None))


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def done(asset, type, completed_on, hours=0, parts=0, vendor=False, rate=82):
    """A work order opened two days before `completed_on` and completed that day, with optional labor and part lines."""
    wo = create_work_order(asset=asset, type=type, priority="normal", problem="Test", opened_on=completed_on - timedelta(days=2), vendor_service=vendor)
    if hours:
        LaborLine.objects.create(work_order=wo, hours=hours, rate=rate)
    if parts:
        PartLine.objects.create(work_order=wo, description="Part", quantity=1, unit_cost=parts)
    change_status(wo, "in_progress", as_of=completed_on - timedelta(days=1))
    change_status(wo, "completed", as_of=completed_on)
    return wo


def contract(reference, type, annual, today, days=100, assets=()):
    c = Contract.objects.create(reference=reference, vendor="Vendor", type=type, start_on=today - timedelta(days=265), end_on=today + timedelta(days=days),
                                annual_cost=annual)
    for a in assets:
        add_asset(c, a)
    return c


@pytest.fixture
def seed(ctx, dept, pump_model, vent, pump):
    """Two categories, all three support models, and work in, on the edge of, and out of the trailing window. `today` is a
    parameter so the rendering tests (which pin the views' clock with `freeze_today`) get the same numbers as the math tests."""

    def _seed(today):
        first_of_month = date(today.year, today.month, 1)
        pump2 = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, acquisition_cost=3000)  # in-house
        contract("SC-OEM", ContractType.OEM, 3800, today, assets=[vent])
        contract("SC-3P", ContractType.THIRD_PARTY, 1200, today, assets=[pump])
        contract("SC-ORPHAN", ContractType.OEM, 1000, today)  # covers no device: cannot be attributed to a category
        contract("SC-OLD", ContractType.OEM, 9999, today, days=-1)  # ended yesterday: excluded everywhere
        done(vent, "repair", first_of_month, hours=2, parts=36)  # 164 + 36 = 200, this month, in-house
        done(vent, "repair", today, hours=1, vendor=True, rate=215)  # 215, this month, vendor time and materials
        done(pump, "pm", today - timedelta(days=100), hours=1)  # 82: PM counts for cosr and contract, not for repair spend
        done(pump2, "repair", first_of_month - timedelta(days=75), hours=1)  # 82, an earlier month inside both windows
        done(pump2, "pm", today - timedelta(days=TRAILING_DAYS), hours=1, rate=50)  # 50, the last day inside the trailing window
        done(pump2, "pm", today - timedelta(days=TRAILING_DAYS + 1), hours=1, rate=70)  # 70, the first day outside it
        done(pump, "repair", today - timedelta(days=200), hours=10)  # 820, outside the trailing 182 days (and the 6 months)
        return pump2

    return _seed


# The seed's service cost inside the trailing window, per category and per source.
VENTS_WO = 415.0  # 200 in-house + 215 vendor
PUMPS_WO = 214.0  # 82 PM + 82 repair + 50 on the window's edge
IN_HOUSE_WO = 414.0
VENDOR_WO = 215.0


def plain(rows):
    return all(isinstance(v, PLAIN) for row in rows for v in row)


# --- cosr: cost of service ratio by category -----------------------------------------------------------------

def test_cosr_math_for_a_fixed_date(seed):
    seed(TODAY)
    r = report_cosr(TODAY)
    assert r["columns"] == ["Category", "Devices", "Acquisition value", "Annual service cost", "Ratio %"] and plain(r["rows"])
    by = {c["label"]: c for c in r["categories"]}
    vents, pumps = by["Ventilators"], by["Infusion pumps"]
    assert vents["devices"] == 1 and vents["acquisition"] == 38000
    assert vents["service"] == pytest.approx(VENTS_WO * ANNUALIZE + 3800)
    assert vents["ratio_pct"] == pytest.approx((VENTS_WO * ANNUALIZE + 3800) / 38000 * 100)
    assert pumps["devices"] == 2 and pumps["acquisition"] == 6200
    # The PM, the older repair, and the work on the edge of the window (182 days ago, not 183), plus the third-party contract on one pump.
    assert pumps["service"] == pytest.approx(PUMPS_WO * ANNUALIZE + 1200)
    assert r["unallocated"] == 1000 and r["benchmark"] == 6.0 and r["no_ratio"] == []
    assert [c["label"] for c in r["categories"]] == ["Infusion pumps", "Ventilators"]  # ratio descending
    assert r["rows"][0] == ["Infusion pumps", 2, 6200.0, pytest.approx(PUMPS_WO * ANNUALIZE + 1200), round(pumps["ratio_pct"], 1)]
    # The breakdown adds up to the fleet-wide figure the Overview tile shows.
    fleet = cost_of_service(TODAY)
    assert r["fleet"] == fleet
    assert fleet["in_house"] == pytest.approx(IN_HOUSE_WO * ANNUALIZE) and fleet["total"] == pytest.approx((VENTS_WO + PUMPS_WO) * ANNUALIZE + 6000)
    assert sum(c["service"] for c in r["categories"]) + r["unallocated"] == pytest.approx(fleet["total"])
    assert sum(c["acquisition"] for c in r["categories"]) == fleet["acquisition"] == 44200
    assert run_report("cosr", TODAY)["rows"] == r["rows"]


def test_cosr_trailing_window_edges(ctx, dept, pump):
    """Work completed exactly 182 days ago is inside the window; 183 days ago is outside. The report and the Overview's figure agree."""
    done(pump, "repair", TODAY - timedelta(days=TRAILING_DAYS), hours=1, rate=50)
    done(pump, "repair", TODAY - timedelta(days=TRAILING_DAYS + 1), hours=1, rate=70)
    r = report_cosr(TODAY)
    assert r["categories"][0]["service"] == pytest.approx(50 * ANNUALIZE) == pytest.approx(cost_of_service(TODAY)["total"])


def test_cosr_splits_a_contract_across_categories_by_acquisition_cost(ctx, dept, vent, pump):
    c = contract("SC-MIX", ContractType.OEM, 4120, TODAY, assets=[vent, pump])  # 38000 : 3200 -> 3800 : 320
    by = {x["label"]: x for x in report_cosr(TODAY)["categories"]}
    assert by["Ventilators"]["service"] == pytest.approx(3800) and by["Infusion pumps"]["service"] == pytest.approx(320)
    assert report_cosr(TODAY)["unallocated"] == 0
    # Only ACTIVE covered devices share the cost: retire the pump and the whole contract lands on Ventilators.
    Asset.objects.filter(pk=pump.pk).update(status="retired")
    r = report_cosr(TODAY)
    by = {x["label"]: x for x in r["categories"]}
    assert list(by) == ["Ventilators"] and by["Ventilators"]["service"] == pytest.approx(4120) and r["unallocated"] == 0
    assert by["Ventilators"]["ratio_pct"] == pytest.approx(4120 / 38000 * 100)
    assert sum(x["service"] for x in r["categories"]) + r["unallocated"] == pytest.approx(r["fleet"]["total"]) == pytest.approx(4120)
    # Retire both: the contract covers no active device, so its cost is unallocated and still part of the fleet total.
    Asset.objects.filter(pk=vent.pk).update(status="retired")
    r = report_cosr(TODAY)
    assert r["categories"] == [] and r["unallocated"] == 4120
    assert sum(x["service"] for x in r["categories"]) + r["unallocated"] == pytest.approx(r["fleet"]["total"]) == pytest.approx(4120)
    # Devices with no acquisition cost cannot carry a share: that money is unallocated, not divided by zero.
    Asset.objects.filter(pk__in=[vent.pk, pump.pk]).update(acquisition_cost=0, status="in_service")
    r = report_cosr(TODAY)
    assert r["unallocated"] == 4120 and all(x["ratio_pct"] is None and x["service"] == 0 for x in r["categories"])
    assert c.cost_share_for(vent) == 0


def test_cosr_categories_with_no_acquisition_value_have_no_ratio(client, signed_in, freeze_today, ctx, dept, vent, pump):
    """A category whose active devices have no recorded acquisition value gets no ratio: it is not a flattering 0.0 at the bottom
    of the chart but a hint line with its service cost, an empty Ratio cell in the CSV, and no bar."""
    Asset.objects.filter(pk=pump.pk).update(acquisition_cost=0)
    done(pump, "repair", TODAY - timedelta(days=3), hours=1)  # 82
    r = report_cosr(TODAY)
    by = {c["label"]: c for c in r["categories"]}
    assert by["Infusion pumps"]["ratio_pct"] is None and by["Infusion pumps"]["service"] == pytest.approx(82 * ANNUALIZE)
    assert by["Ventilators"]["ratio_pct"] == 0.0  # no service cost on a real acquisition value is a genuine zero
    assert [c["label"] for c in r["categories"]] == ["Ventilators", "Infusion pumps"]  # no ratio sorts last, whatever the spend
    assert r["rows"][1][:2] == ["Infusion pumps", 1] and r["rows"][1][4] is None and r["rows"][0][4] == 0.0 and plain(r["rows"])
    assert [c["label"] for c in r["no_ratio"]] == ["Infusion pumps"]
    assert sum(c["service"] for c in r["categories"]) + r["unallocated"] == pytest.approx(r["fleet"]["total"])
    freeze_today(TODAY)
    signed_in("director")
    body = client.get("/reports/cosr/").content.decode()
    assert "<title>Ventilators: 0.0%</title>" in body and "<title>Infusion pumps" not in body
    assert "Infusion pumps: $164 annual service cost on devices with no recorded acquisition value (no ratio)." in body
    csv = client.get("/reports/cosr.csv").content.decode().splitlines()
    assert csv[1] == "Ventilators,1,38000.00,0.00,0.00" and csv[2] == "Infusion pumps,1,0.00,164.45,"
    # No category has a ratio: no chart at all, and the empty state says why.
    Asset.objects.filter(pk=vent.pk).update(acquisition_cost=0)
    body = client.get("/reports/cosr/").content.decode()
    assert '<svg class="chart"' not in body and "No active device has a recorded acquisition value, so no ratio can be shown." in body
    assert "No active devices." not in body and "Ventilators: $0 annual service cost" in body


def test_cosr_work_on_a_retired_only_category_is_unallocated(ctx, dept, vent, pump):
    done(vent, "repair", TODAY - timedelta(days=3), hours=1)
    Asset.objects.filter(pk=vent.pk).update(status="retired")
    r = report_cosr(TODAY)
    assert [c["label"] for c in r["categories"]] == ["Infusion pumps"]
    assert r["unallocated"] == pytest.approx(82 * ANNUALIZE)
    assert sum(c["service"] for c in r["categories"]) + r["unallocated"] == pytest.approx(r["fleet"]["total"])


def test_cosr_ignores_other_tenants(seed, other_tenant):
    seed(TODAY)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Foreign device", category="Foreign")
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"), acquisition_cost=999999)
        contract("THEIRS", ContractType.OEM, 50000, TODAY, assets=[a])
        done(a, "repair", TODAY - timedelta(days=3), hours=10, rate=100)
    r = report_cosr(TODAY)
    assert "Foreign" not in [c["label"] for c in r["categories"]]
    assert r["fleet"]["acquisition"] == 44200 and r["unallocated"] == 1000
    assert r["fleet"]["total"] == pytest.approx((VENTS_WO + PUMPS_WO) * ANNUALIZE + 6000)


def test_cosr_renders_the_stats_chart_and_hints(client, signed_in, freeze_today, seed):
    freeze_today(TODAY)
    signed_in("director")
    seed(TODAY)
    r = client.get("/reports/cosr/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "fleet-wide, annualized" in body and "annual service cost" in body and "acquisition value in service" in body
    assert "$44.2k" in body and "$7,261" in body
    assert 'aria-label="Cost of service ratio by category"' in body and "<title>Infusion pumps: 26.3%</title>" in body and "Ventilators: 12.2%" in body
    assert "<title>Benchmark: 6.0%</title>" in body and 'stroke="var(--crit)"' in body
    assert "Red marker: 6% benchmark midpoint." in body and "$1,000 of annual service cost is on contracts that cover no active devices" in body
    assert "no recorded acquisition value" not in body
    csv = client.get("/reports/cosr.csv").content.decode().splitlines()
    assert csv[0] == "Category,Devices,Acquisition value,Annual service cost,Ratio %" and csv[1].startswith("Infusion pumps,2,6200.00,")


def test_cosr_on_an_empty_tenant(client, signed_in, ctx):
    r = report_cosr(TODAY)
    assert r["rows"] == [] and r["categories"] == [] and r["unallocated"] == 0 and r["no_ratio"] == []
    assert r["fleet"]["total"] == 0 and r["fleet"]["acquisition"] == 0 and r["fleet"]["ratio_pct"] == 0
    signed_in("director")
    body = client.get("/reports/cosr/").content.decode()
    assert "No active devices." in body and "0.0%" in body and "is not shown by category" not in body and '<svg class="chart"' not in body


# --- spend: repair spend trend --------------------------------------------------------------------------------

def test_spend_math_for_a_fixed_date(seed):
    seed(TODAY)
    r = report_spend(TODAY)
    assert r["columns"] == ["Month", "Labor", "Parts", "Total"] and plain(r["rows"])
    assert [(m["year"], m["month"]) for m in r["months"]] == [(2026, 4), (2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)]
    assert [m["label"] for m in r["months"]] == ["Apr 2026", "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026"]
    sep, jun = r["months"][-1], r["months"][2]
    assert sep == {"year": 2026, "month": 9, "label": "Sep 2026", "labor": 379.0, "parts": 36.0, "total": 415.0}
    assert jun["labor"] == 82 and jun["parts"] == 0 and jun["total"] == 82  # Jun 18: the in-house repair on the second pump
    assert sum(m["total"] for m in r["months"][:2] + r["months"][3:5]) == 0  # the PMs and the repair 200 days back do not count
    assert r["month_to_date"] == 415 == overview_kpis(2026, 9, TODAY)["repair_spend"]
    assert r["six_months"] == 497 and r["monthly_average"] == pytest.approx(497 / 6)
    assert r["rows"][-1] == ["Sep 2026", 379.0, 36.0, 415.0]


def test_spend_ignores_other_tenants(seed, other_tenant):
    seed(TODAY)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Foreign device", category="Foreign")
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"))
        done(a, "repair", TODAY - timedelta(days=3), hours=10, rate=100, parts=500)
    r = report_spend(TODAY)
    assert r["month_to_date"] == 415 and r["six_months"] == 497


def test_spend_renders_the_stats_and_chart(client, signed_in, freeze_today, seed):
    freeze_today(TODAY)
    signed_in("director")
    seed(TODAY)
    r = client.get("/reports/spend/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "month to date (partial month)" in body and "last 6 months" in body and "monthly average" in body
    assert '<div class="v">$415</div>' in body and '<div class="v">$497</div>' in body and '<div class="v">$83</div>' in body
    assert 'aria-label="Repair spend by month"' in body and "Labor</span>" in body and "Parts</span>" in body
    assert "<title>Sep · Parts: $36</title>" in body and "No repairs completed" not in body
    assert "Repair work orders only; PM labor and service contracts are excluded." in body
    csv = client.get("/reports/spend.csv").content.decode().splitlines()
    assert csv[0] == "Month,Labor,Parts,Total" and len(csv) == 7 and csv[-1] == "Sep 2026,379.00,36.00,415.00"


def test_spend_on_an_empty_tenant(client, signed_in, ctx):
    r = report_spend(TODAY)
    assert len(r["months"]) == 6 and r["month_to_date"] == 0 and r["six_months"] == 0 and r["monthly_average"] == 0
    assert all(row[1:] == [0.0, 0.0, 0.0] for row in r["rows"])
    signed_in("director")
    body = client.get("/reports/spend/").content.decode()
    assert '<div class="v">$0</div>' in body and "last 6 months" in body  # the stats block stays
    assert "No repairs completed in the last 6 months." in body and '<svg class="chart"' not in body and 'aria-label="Repair spend by month"' not in body


# --- contract: contract vs in-house ---------------------------------------------------------------------------

def test_contract_math_for_a_fixed_date(seed):
    seed(TODAY)
    r = report_contract(TODAY)
    assert r["columns"] == ["Support model", "Devices", "Acquisition value", "Annual cost", "Ratio %"] and plain(r["rows"])
    assert [s["label"] for s in r["support"]] == ["In-house", "OEM contract", "Third-party"]
    in_house, oem, third = r["support"]
    # The second pump: its repair and the PM on the edge of the window (182 days ago); the one at 183 days is out.
    assert (in_house["devices"], in_house["acquisition"]) == (1, 3000) and in_house["annual"] == pytest.approx(132 * ANNUALIZE)
    assert (oem["devices"], oem["acquisition"]) == (1, 38000)
    assert oem["annual"] == pytest.approx(VENTS_WO * ANNUALIZE + 3800 + 1000)  # the orphan contract is OEM
    assert (third["devices"], third["acquisition"]) == (1, 3200) and third["annual"] == pytest.approx(82 * ANNUALIZE + 1200)
    assert third["ratio_pct"] == pytest.approx((82 * ANNUALIZE + 1200) / 3200 * 100)
    assert r["ended_contract_devices"] == 0
    fleet = cost_of_service(TODAY)
    assert r["fleet"] == fleet
    assert fleet["in_house"] == pytest.approx(IN_HOUSE_WO * ANNUALIZE) and fleet["vendor_tm"] == pytest.approx(VENDOR_WO * ANNUALIZE)
    assert fleet["contracts"] == 6000
    assert sum(s["annual"] for s in r["support"]) == pytest.approx(fleet["total"])
    assert r["rows"][1][:4] == ["OEM contract", 1, 38000.0, pytest.approx(VENTS_WO * ANNUALIZE + 4800)]


def test_contract_counts_devices_on_ended_contracts_as_in_house(client, signed_in, freeze_today, ctx, dept, vent, pump):
    """The stored support type follows the contract even after it ends; the report groups by the LIVE contract instead, so a
    device whose contract has ended (and its work) sits with in-house until the contract is renewed, and the hint says so."""
    ended = contract("SC-ENDED", ContractType.OEM, 5000, TODAY, days=-1, assets=[vent])
    contract("SC-LIVE", ContractType.THIRD_PARTY, 1200, TODAY, assets=[pump])
    done(vent, "repair", TODAY - timedelta(days=3), hours=1)  # 82 on the ended-contract vent
    done(pump, "repair", TODAY - timedelta(days=3), hours=2)  # 164 on the live third-party pump
    vent.refresh_from_db()
    assert vent.support_type == SupportType.OEM_CONTRACT  # the stored value the report must not trust
    r = report_contract(TODAY)
    in_house, oem, third = r["support"]
    assert (in_house["devices"], in_house["acquisition"]) == (1, 38000) and in_house["annual"] == pytest.approx(82 * ANNUALIZE)
    assert (oem["devices"], oem["acquisition"], oem["annual"], oem["ratio_pct"]) == (0, 0, 0, 0)
    assert (third["devices"], third["acquisition"]) == (1, 3200) and third["annual"] == pytest.approx(164 * ANNUALIZE + 1200)
    assert r["ended_contract_devices"] == 1
    fleet = cost_of_service(TODAY)
    assert r["fleet"] == fleet and fleet["contracts"] == 1200 and sum(s["annual"] for s in r["support"]) == pytest.approx(fleet["total"])
    freeze_today(TODAY)
    signed_in("director")
    body = client.get("/reports/contract/").content.decode()
    assert "1 device is on contracts that have ended and is counted as in-house until the contract is renewed." in body
    assert '<td>In-house</td><td class="num">1</td><td class="num">$38.0k</td>' in body and '<td>OEM contract</td><td class="num">0</td>' in body
    # Renew the contract and the vent is back under OEM, with no hint.
    Contract.objects.filter(pk=ended.pk).update(end_on=TODAY + timedelta(days=365))
    r = report_contract(TODAY)
    in_house, oem, third = r["support"]
    assert (in_house["devices"], in_house["annual"]) == (0, 0) and (oem["devices"], oem["acquisition"]) == (1, 38000)
    assert oem["annual"] == pytest.approx(82 * ANNUALIZE + 5000) and r["ended_contract_devices"] == 0
    assert sum(s["annual"] for s in r["support"]) == pytest.approx(cost_of_service(TODAY)["total"])
    assert "counted as in-house until the contract is renewed" not in client.get("/reports/contract/").content.decode()


def test_contract_trailing_window_edges(ctx, dept, pump):
    done(pump, "repair", TODAY - timedelta(days=TRAILING_DAYS), hours=1, rate=50)
    done(pump, "repair", TODAY - timedelta(days=TRAILING_DAYS + 1), hours=1, rate=70)
    r = report_contract(TODAY)
    assert r["support"][0]["annual"] == pytest.approx(50 * ANNUALIZE) == pytest.approx(cost_of_service(TODAY)["total"])
    assert r["support"][1]["annual"] == 0 and r["support"][2]["annual"] == 0


def test_contract_ignores_other_tenants(seed, other_tenant):
    seed(TODAY)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Foreign device", category="Foreign")
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"), acquisition_cost=999999)
        contract("THEIRS", ContractType.THIRD_PARTY, 50000, TODAY, assets=[a])
        contract("THEIRS-OLD", ContractType.OEM, 50000, TODAY, days=-1, assets=[a])
        done(a, "repair", TODAY - timedelta(days=3), hours=10, rate=100)
    r = report_contract(TODAY)
    assert [s["devices"] for s in r["support"]] == [1, 1, 1] and r["ended_contract_devices"] == 0
    assert r["support"][2]["annual"] == pytest.approx(82 * ANNUALIZE + 1200) and r["fleet"]["contracts"] == 6000


def test_contract_renders_the_donut_table_and_hint(client, signed_in, freeze_today, seed):
    freeze_today(TODAY)
    signed_in("director")
    seed(TODAY)
    r = client.get("/reports/contract/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'aria-label="Service spend by source"' in body and "per year" in body and "$7,261" in body
    assert "In-house labor and parts&nbsp;<b>$830</b>" in body and "Vendor time and materials&nbsp;<b>$431</b>" in body
    assert "Service contracts&nbsp;<b>$6,000</b>" in body
    assert '<td>OEM contract</td><td class="num">1</td><td class="num">$38.0k</td><td class="num">$5,632</td><td class="num">14.8%</td>' in body
    assert '<td>In-house</td><td class="num">1</td><td class="num">$3,000</td><td class="num">$265</td><td class="num">8.8%</td>' in body
    assert "devices with no contract, or whose contract has ended, count as in-house." in body and "No service cost recorded yet." not in body
    assert "counted as in-house until the contract is renewed" not in body  # no device is on an ended contract in the seed
    csv = client.get("/reports/contract.csv").content.decode().splitlines()
    assert csv[0] == "Support model,Devices,Acquisition value,Annual cost,Ratio %" and len(csv) == 4 and csv[1].startswith("In-house,1,3000.00,")


def test_contract_on_an_empty_tenant(client, signed_in, ctx):
    r = report_contract(TODAY)
    assert [s["label"] for s in r["support"]] == ["In-house", "OEM contract", "Third-party"]
    assert all(s["devices"] == 0 and s["acquisition"] == 0 and s["annual"] == 0 and s["ratio_pct"] == 0 for s in r["support"])
    assert r["fleet"]["total"] == 0 and r["ended_contract_devices"] == 0
    signed_in("director")
    body = client.get("/reports/contract/").content.decode()
    assert "No service cost recorded yet." in body and 'aria-label="Service spend by source"' not in body
    assert '<td>Third-party</td><td class="num">0</td><td class="num">$0</td><td class="num">$0</td><td class="num">0.0%</td>' in body
