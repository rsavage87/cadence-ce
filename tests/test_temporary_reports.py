"""
Slice 29, wave 2B: rentals, vendor loaners, and demo units in the figures. The counting rule (equipment.services.OWNED /
WORK_ORDER_OWNED): what is about CE's program and the facility's own fleet (PM compliance, AEM evidence, COSR, contract vs in-house,
MTBF, uptime, replacement, the PM library's counts, the Overview's active devices) never moves for a temporary device's repairs,
downtime, or missing status; what is about CE's work (open and overdue work, MTTR, repair spend, technicians) counts it. Then the
Overview's attention lines (past due back; a vendor loaner whose device is back), the custom report's Status ("Returned to owner")
and Whose, and the devices API's read-only stay fields with its two wave 1 guards (no PM moved onto a temporary device; a contract's
add-by-model never reports one as moved).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.serializers import AssetSerializer
from apps.contracts.models import Contract, ContractType
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, Ownership, ReturnCleaning, ReturnData
from apps.pm import aem
from apps.pm.schedule import pm_library
from apps.reports import custom
from apps.reports.cost import report_contract, report_cosr, report_spend
from apps.reports.fleet import report_compliance, report_mtbf, report_replace
from apps.reports.operations import report_tech
from apps.reports.services import ATTENTION_TEMPORARY_LIMIT, attention_items, cost_of_service, overview_kpis, temporary_attention
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, PartLine, WoType
from apps.workorders.services import assign, change_status, create_work_order, no_pm_message

S = AssetStatus
ASSETS, WOS = "/api/v1/assets/", "/api/v1/work-orders/"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def on_site(model, dept, tag, *, today, kind=Ownership.RENTAL, arrived_days=20, **kw):
    """A temporary device entered as already on site (EXISTING): in service, no inspection."""
    values = {"tag": tag, "device_model": model, "department": dept, "kind": kind, "owner": "Acme Rentals", "serial": f"SN-{tag}",
              "owner_reference": "RA-2026-0042", "added_as": AddedAs.EXISTING, "arrived_on": today - timedelta(days=arrived_days),
              "owner_pm_due_on": today + timedelta(days=90), "today": today}
    return eq.add_temporary_device(**{**values, **kw})


def repair(asset, opened, completed=None, *, hours=None, part=None, technician=None):
    w = create_work_order(asset=asset, type=WoType.REPAIR, priority="normal", problem="Alarm", opened_on=opened, due_on=opened + timedelta(days=5))
    if technician is not None:
        assign(w, technician=technician)
    if hours:
        LaborLine.objects.create(work_order=w, technician=technician, hours=hours, rate=82)
    if part:
        PartLine.objects.create(work_order=w, description="Part", quantity=1, unit_cost=part)
    if completed:
        change_status(w, "in_progress", as_of=opened)
        change_status(w, "completed", as_of=completed)
    return w


def program_figures(today, models):
    """Everything the counting rule keeps to our devices: the figures that must never move for a temporary device."""
    k = overview_kpis(today.year, today.month, today)
    return {
        "overview": {key: k[key] for key in ("active_devices", "downtime_days", "uptime_pct", "cost_of_service", "pm_on_time",
                                             "pm_on_time_life_support")},
        "cost_of_service": cost_of_service(today),
        "compliance": report_compliance(today)["rows"],
        "mtbf": report_mtbf(today)["rows"],
        "replace": report_replace(today)["rows"],
        "cosr": {key: report_cosr(today)[key] for key in ("rows", "fleet", "unallocated")},
        "contract": {key: report_contract(today)[key] for key in ("rows", "fleet")},
        "aem": [aem.evidence(dm, today) for dm in models],
        "library": [(row["device_model"].pk, row["devices"]) for row in pm_library()],
    }


def work_figures(today):
    k = overview_kpis(today.year, today.month, today)
    return {"open": k["open_work_orders"], "closed": k["repairs_closed"], "mttr": k["mttr_days"], "spend": k["repair_spend"],
            "six_months": report_spend(today)["six_months"], "tech": report_tech(today)["rows"]}


# --- the counting rule ------------------------------------------------------------------------------------------------------------

def test_a_temporary_devices_repairs_downtime_and_missing_status_never_move_the_program_figures(ctx, today, dept, vent, pump, pump_model,
                                                                                                 vent_model, techs):
    ct = Contract.objects.create(reference="SC-1", vendor="BD", type=ContractType.OEM, start_on=today - timedelta(days=100),
                                 end_on=today + timedelta(days=200), annual_cost=Decimal("1200"))
    from apps.contracts import services as contracts

    contracts.add_asset(ct, pump)
    repair(pump, today - timedelta(days=2), today, hours=Decimal("2"), part=Decimal("100"), technician=techs["dana"])  # ours
    before, work_before = program_figures(today, [pump_model, vent_model]), work_figures(today)

    rental = on_site(pump_model, dept, "R-1", today=today)
    demo = on_site(vent_model, dept, "D-1", today=today, kind=Ownership.DEMO, owner="Acme Demo")
    eq.set_status(demo, S.MISSING, today=today)  # a life-support unit lost: never "overdue" on our compliance
    repair(rental, today - timedelta(days=3), today, hours=Decimal("5"), part=Decimal("900"), technician=techs["dana"])
    repair(rental, today - timedelta(days=1))  # still open: CE's work
    assert program_figures(today, [pump_model, vent_model]) == before

    after = work_figures(today)
    assert after["open"] == work_before["open"] + 1 and after["closed"] == work_before["closed"] + 1
    assert after["mttr"] != work_before["mttr"] and after["spend"] == pytest.approx(work_before["spend"] + 5 * 82 + 900)
    assert after["six_months"] == pytest.approx(work_before["six_months"] + 5 * 82 + 900)
    dana = next(r for r in after["tech"] if r[0] == "Dana Whitfield")
    assert dana[2] == next(r for r in work_before["tech"] if r[0] == "Dana Whitfield")[2] + 1  # closed: the rental's repair is her work


def test_a_returned_device_is_off_the_figures_and_the_overview_counts_ours(ctx, today, dept, vent, pump, pump_model):
    rental = on_site(pump_model, dept, "R-1", today=today)
    assert overview_kpis(today.year, today.month, today)["active_devices"] == 2  # ours: the rental is not in the tile
    eq.return_to_owner(rental, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED, today=today)
    k = overview_kpis(today.year, today.month, today)
    assert k["active_devices"] == 2 and k["cost_of_service"]["acquisition"] == pytest.approx(38000 + 3200)
    assert [r[1] for r in report_compliance(today)["rows"]] == [1, 1, 0, 0]


def test_aem_evidence_counts_a_kept_device_from_the_day_it_was_kept(ctx, today, dept, pump, pump_model):
    unit = on_site(pump_model, dept, "R-1", today=today, arrived_days=200)
    repair(unit, today - timedelta(days=100), today - timedelta(days=99))  # while it was the owner's: never the model's history
    before = aem.evidence(pump_model, today)
    assert before["devices_active"] == 1 and before["repairs"] == 0
    eq.keep_temporary_device(unit, acquisition_cost=Decimal("2500"), today=today)
    kept = aem.evidence(pump_model, today)
    assert kept["devices_active"] == 2 and kept["repairs"] == 0 and kept["device_years"] == before["device_years"]  # ours from today
    repair(unit, today, today)  # once ours, its failures are the model's
    assert aem.evidence(pump_model, today)["repairs"] == 1
    assert aem.evidence(pump_model, today - timedelta(days=1))["devices_active"] == 1  # not ours yet the day before


def test_the_pm_library_counts_ours(ctx, today, dept, pump, pump_model):
    on_site(pump_model, dept, "R-1", today=today)
    on_site(pump_model, dept, "R-2", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    assert {row["device_model"].pk: row["devices"] for row in pm_library()}[pump_model.pk] == 1


# --- the Overview's attention list ------------------------------------------------------------------------------------------------

def test_temporary_devices_past_due_back_are_listed_longest_overdue_first_and_capped(ctx, today, dept, pump_model, vent):
    for i in range(ATTENTION_TEMPORARY_LIMIT + 2):
        on_site(pump_model, dept, f"R-{i}", today=today, due_back_on=today - timedelta(days=i + 1))
    on_site(pump_model, dept, "R-OK", today=today, due_back_on=today)  # due back today: not past
    gone = on_site(pump_model, dept, "R-GONE", today=today, due_back_on=today - timedelta(days=15))
    eq.return_to_owner(gone, cleaning=ReturnCleaning.LABELED, data=ReturnData.NONE_STORED, today=today)
    lines = temporary_attention(today)
    assert [(i["asset"], i["right"], i["rail"]) for i in lines] == [
        (f"R-{n}", f"Past due back {n + 1} d", "warn") for n in reversed(range(2, ATTENTION_TEMPORARY_LIMIT + 2))]
    assert lines[0]["title"] == "R-6 · Infusion pump" and lines[0]["sub"].startswith("Rental from Acme Rentals · ICU · due back ")
    assert "RA-2026-0042" not in repr(lines)  # never the owner's reference
    assert [i for i in attention_items(today) if i.get("asset", "").startswith("R-")] == lines


def test_a_vendor_loaner_is_listed_once_its_device_is_back(ctx, today, dept, pump, pump_model, vent):
    eq.set_status(pump, S.OUT_OF_SERVICE, today=today)  # out for vendor repair
    loaner = on_site(pump_model, dept, "L-1", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    assert temporary_attention(today) == []
    eq.set_status(pump, S.IN_SERVICE, today=today)
    [line] = temporary_attention(today)
    assert (line["asset"], line["sub"], line["right"]) == ("L-1", "Vendor loaner from BD for CE-10002, which is back in service", "Return the loaner")
    eq.set_status(pump, S.RETIRED, today=today)
    assert temporary_attention(today)[0]["sub"] == "Vendor loaner from BD for CE-10002, which is retired"
    eq.return_to_owner(loaner, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED, today=today)
    assert temporary_attention(today) == []


def test_the_attention_list_reads_a_fixed_number_of_queries(ctx, today, dept, pump_model, vent):
    def count(n):
        for i in range(n):
            ours = Asset.objects.create(tag=f"O-{n}-{i}", device_model=pump_model, department=dept, next_pm_on=today + timedelta(days=90))
            on_site(pump_model, dept, f"L-{n}-{i}", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=ours)
            on_site(pump_model, dept, f"R-{n}-{i}", today=today, due_back_on=today - timedelta(days=1))
        each = min(Asset.objects.filter(tag__startswith="R-").count(), ATTENTION_TEMPORARY_LIMIT)
        with CaptureQueriesContext(connection) as q:
            lines = temporary_attention(today)
        assert len(lines) == 2 * each
        return len(q)

    assert count(1) == count(3)


# --- the custom report --------------------------------------------------------------------------------------------------------------

def run(columns, filters=None, group_by="", sort="", *, today):
    return custom.run(custom.clean_definition("devices", columns, filters or {}, group_by, sort), today)


def test_the_custom_report_says_returned_to_owner_and_whose(ctx, today, dept, vent, pump, pump_model):
    eq.set_status(vent, S.RETIRED, today=today)
    back = on_site(pump_model, dept, "R-1", today=today)
    eq.return_to_owner(back, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED, today=today)
    on_site(pump_model, dept, "L-1", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    r = run(["tag", "status", "ownership", "pm_interval"], today=today)
    assert r["columns"] == ["Tag", "Status", "Whose", "PM interval (months)"]
    assert r["rows"] == [["CE-10001", "Retired", "Ours", 6], ["CE-10002", "In service", "Ours", 18], ["L-1", "In service", "Vendor loaner", None],
                         ["R-1", "Returned to owner", "Rental", None]]
    assert [row[0] for row in run(["tag"], {"status": ["returned"]}, today=today)["rows"]] == ["R-1"]
    assert [row[0] for row in run(["tag"], {"status": ["retired"]}, today=today)["rows"]] == ["CE-10001"]  # retired means ours
    assert [row[0] for row in run(["tag"], {"ownership": ["loaner", "rental"]}, today=today)["rows"]] == ["L-1", "R-1"]
    grouped = run(["tag"], group_by="status", today=today)
    assert grouped["rows"] == [["In service", 2], ["Retired", 1], ["Returned to owner", 1]]
    assert [row[0] for row in run(["tag", "status"], sort="-status", today=today)["rows"]] == ["R-1", "CE-10001", "CE-10002", "L-1"]
    assert run(["tag"], group_by="ownership", today=today)["rows"] == [["Ours", 2], ["Rental", 1], ["Vendor loaner", 1]]
    assert "Status: Returned to owner" in custom.describe(custom.DEVICES, custom.clean_definition("devices", ["tag"], {"status": ["returned"]}))


def test_the_custom_report_never_offers_the_owners_reference():
    for spec in custom.SOURCES.values():
        paths = [c.value for c in spec.columns if isinstance(c.value, str)] + [f.lookup for f in spec.filters]
        assert not [p for p in paths if p.split("__")[-1] in ("owner_reference", "owner")], spec.key
    assert set(custom.DeviceStatus.values) == set(AssetStatus.values) | {"returned"}
    assert dict(custom.DeviceStatus.choices)["returned"] == eq.RETURNED_LABEL


# --- the devices API: read-only stay fields -----------------------------------------------------------------------------------------

@pytest.fixture
def person(client, make_user):
    n = iter(range(1000))

    def _as(slug, department=""):
        user = make_user(slug, username=f"{slug}-{next(n)}@riverside.example")
        user.department = department
        user.save()
        client.force_login(user)
        return user

    return _as


def api(client, method, url, body=None):
    return getattr(client, method)(url, body, content_type="application/json") if body is not None else getattr(client, method)(url)


def test_the_api_shows_the_stay_read_only(client, person, today, dept, pump, pump_model):
    loaner = on_site(pump_model, dept, "L-1", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump,
                     due_back_on=today + timedelta(days=9))
    person("technician")
    got = api(client, "get", f"{ASSETS}{loaner.id}/").json()
    assert {k: got[k] for k in ("ownership", "ownership_label", "owner", "owner_reference", "arrived_on", "due_back_on", "owner_pm_due_on",
                                "returned_on", "kept_on", "stands_in_for", "stands_in_for_tag", "status_label", "support_type")} == {
        "ownership": "loaner", "ownership_label": "Vendor loaner", "owner": "BD", "owner_reference": "RA-2026-0042",
        "arrived_on": (today - timedelta(days=20)).isoformat(), "due_back_on": (today + timedelta(days=9)).isoformat(),
        "owner_pm_due_on": (today + timedelta(days=90)).isoformat(), "returned_on": None, "kept_on": None, "stands_in_for": str(pump.id),
        "stands_in_for_tag": "CE-10002", "status_label": "In service", "support_type": "owner"}
    ours = api(client, "get", f"{ASSETS}{pump.id}/").json()
    assert (ours["ownership"], ours["owner"], ours["stands_in_for"], ours["arrived_on"]) == ("owned", "", None, None)
    stay = {"ownership", "owner", "owner_reference", "arrived_on", "due_back_on", "owner_pm_due_on", "returned_on", "kept_on", "stands_in_for",
            "stands_in_for_tag", "ownership_label"}
    assert stay <= {name for name, f in AssetSerializer().fields.items() if f.read_only}


def test_the_api_adds_ours_and_refuses_a_stay_it_cannot_write(client, person, today, dept, pump, pump_model):
    loaner = on_site(pump_model, dept, "L-1", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    person("director")
    body = {"tag": "CE-777", "device_model": str(pump_model.id), "department": str(dept.id)}
    r = api(client, "post", ASSETS, {**body, "ownership": "rental", "owner": "Acme"})
    assert r.status_code == 400 and set(r.json()) == {"ownership", "owner"} and "Add rental or loaner" in r.json()["ownership"][0]
    r = api(client, "post", ASSETS, {**body, "ownership": "owned"})
    assert r.status_code == 201 and r.json()["ownership"] == "owned" and Asset.objects.get(tag="CE-777").ownership == Ownership.OWNED
    got = api(client, "get", f"{ASSETS}{loaner.id}/").json()
    sent = {k: got[k] for k in AssetSerializer.TEMPORARY_FIELDS}
    assert api(client, "patch", f"{ASSETS}{loaner.id}/", {**sent, "room": "12"}).status_code == 200  # sending back a GET's values is fine
    for field, value in (("owner", "Other"), ("due_back_on", today.isoformat()), ("stands_in_for", None), ("ownership", "owned")):
        r = api(client, "patch", f"{ASSETS}{loaner.id}/", {field: value})
        assert r.status_code == 400 and list(r.json()) == [field], (field, r.json())
    loaner.refresh_from_db()
    assert (loaner.owner, loaner.ownership, loaner.stands_in_for_id, loaner.room) == ("BD", Ownership.LOANER, pump.pk, "12")


def test_a_scoped_user_never_sees_a_loaners_device_outside_their_share(client, person, today, dept, pump, pump_model):
    ed = Department.objects.create(name="ED")
    loaner = on_site(pump_model, ed, "L-1", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)  # ours is in the ICU
    person("requester", department="ED")
    [row] = api(client, "get", ASSETS).json()["results"]
    assert (row["tag"], row["stands_in_for"], row["stands_in_for_tag"]) == ("L-1", None, None)
    got = api(client, "get", f"{ASSETS}{loaner.id}/").json()
    assert (got["stands_in_for"], got["stands_in_for_tag"]) == (None, None)
    person("requester", department="ICU")
    assert [r["tag"] for r in api(client, "get", ASSETS).json()["results"]] == ["CE-10002"]
    Asset.objects.filter(pk=loaner.pk).update(department=dept)  # the loaner goes up to the ICU, where its pump is
    by_tag = {r["tag"]: r for r in api(client, "get", ASSETS).json()["results"]}
    assert (by_tag["L-1"]["stands_in_for"], by_tag["L-1"]["stands_in_for_tag"]) == (str(pump.id), "CE-10002")


def test_the_device_list_reads_a_fixed_number_of_queries_with_loaners(client, person, today, dept, pump_model):
    person("requester", department="ICU")

    def count(n):
        for i in range(n):
            ours = Asset.objects.create(tag=f"Q-{n}-{i}", device_model=pump_model, department=dept, next_pm_on=today + timedelta(days=90))
            on_site(pump_model, dept, f"L-{n}-{i}", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=ours)
        with CaptureQueriesContext(connection) as q:
            results = api(client, "get", ASSETS).json()["results"]
        assert all(r["stands_in_for_tag"] for r in results if r["tag"].startswith("L-"))
        return len(q)

    assert count(2) == count(5)


# --- the two wave 1 guards in the API ------------------------------------------------------------------------------------------------

def test_an_api_edit_never_puts_a_pm_on_a_temporary_device(client, person, today, dept, vent, pump_model):
    rental = on_site(pump_model, dept, "R-1", today=today)
    fix = create_work_order(asset=rental, type=WoType.REPAIR, priority="normal", problem="Alarm")
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="PM")
    person("director")
    r = api(client, "patch", f"{WOS}{fix.id}/", {"type": WoType.PM})
    assert r.status_code == 400 and r.json() == {"type": [no_pm_message(rental)]}
    r = api(client, "patch", f"{WOS}{pm.id}/", {"asset": str(rental.id)})
    assert r.status_code == 400 and r.json() == {"asset": [no_pm_message(rental)]}
    r = api(client, "patch", f"{WOS}{fix.id}/", {"type": WoType.PM, "asset": str(vent.id)})  # a PM on ours: as before
    assert r.status_code == 200, r.json()
    fix.refresh_from_db()
    pm.refresh_from_db()
    assert (fix.type, fix.asset_id, pm.asset_id) == (WoType.PM, vent.pk, vent.pk)


def test_a_contracts_add_by_model_never_reports_a_temporary_device(client, person, today, dept, pump, pump_model):
    on_site(pump_model, dept, "R-1", today=today)
    ct = Contract.objects.create(reference="SC-9", vendor="BD", type=ContractType.OEM, start_on=today, end_on=today + timedelta(days=365))
    person("manager")
    r = api(client, "post", f"/api/v1/contracts/{ct.id}/add_assets/", {"device_model": str(pump_model.id)})
    assert r.status_code == 200 and r.json()["added"] == 1 and [d["tag"] for d in r.json()["devices"]] == ["CE-10002"]
    assert r.json()["device_count"] == 1 and Asset.objects.get(tag="R-1").contract_id is None


def test_isolation_the_stay_and_the_figures_stay_in_their_facility(ctx, today, dept, pump_model, other_tenant):
    on_site(pump_model, dept, "R-1", today=today, due_back_on=today - timedelta(days=2))
    with tenant_context(other_tenant):
        assert temporary_attention(today) == [] and Asset.objects.filter(tag="R-1").count() == 0
        assert run(["tag"], today=today)["rows"] == []
