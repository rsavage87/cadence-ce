"""
Slice 34, wave 1B: a contracted service vendor's review figures (apps/evaluations/vendor_figures.py), read only.

Each figure on a small facility: the contracts from Contract history (a price change partway, a renewal's days, a notes edit that is
one version, a deleted contract, a renamed vendor, a contract entered after the period, devices covered today); the jobs by type and by
who recorded the completion (the vendor's account: a company-scoped role), imported apart; time with the vendor from the last "Assigned
to vendor" row naming it (else opened) to completed, split by priority and by who, and device downtime apart; PMs assigned to the vendor
on time by the window in force at the period's end, late by side, still pending, and PMs on covered devices not assigned to it; repeat
repairs counted only for a device failure or an unknown finding, matched to the vendor's last repair, provisional for 30 days; no problem
found; documentation split by who; staff evidence (slice 31's rule); labor and parts. Two names one vendor; the real services' status
rows; a fixed number of queries at two sizes; another facility never read; nothing a requester typed, no person, no tag, never uptime or
response as a figure; the cut all or nothing per role; under row-level security as the runtime role.
"""
import json
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.contracts import services as cs
from apps.contracts.models import Contract
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.evaluations import vendor_figures as vf
from apps.facility import services as fac
from apps.facility.models import FacilitySettings, PmWindow
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.vendors import services as vs
from apps.vendors.models import Covers, TrainingSource
from apps.workorders.completion import complete_work_order
from apps.workorders.models import (
    LaborLine,
    LateReason,
    PartLine,
    Priority,
    RepairCheck,
    RepairFinding,
    Source,
    WorkOrder,
    WorkOrderStatusHistory,
    WoStatus,
    WoType,
)
from apps.workorders.services import assign, change_status, create_work_order

DAY = timedelta(days=1)
SECRET = "Zq7Canary bed 4"  # what a requester typed: never in the figures
VENDOR = "Philips"
KEY = "philips"


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    """A 12-month period that ended 40 days ago: its repeat repairs are final."""
    end = today - 40 * DAY
    return add_months(end, -12) + DAY, end


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


@pytest.fixture
def monitors(ctx, dept, monitor_model):
    """Five Philips monitors, m1..m5."""
    return {f"m{i}": Asset.objects.create(tag=f"CE-2000{i}", device_model=monitor_model, department=dept) for i in range(1, 6)}


@pytest.fixture
def vendor_account(make_user):
    """The vendor's own account: the default vendor role sees only work orders assigned to its company."""
    u = make_user("vendor")
    u.company = VENDOR
    u.save(update_fields=["company"])
    return u


@pytest.fixture
def ce(make_user):
    return make_user("manager")


@pytest.fixture
def dana(ctx):
    return vs.add_engineer(name="Dana Kim", company=VENDOR)


def status_row(wo, to, *, on, frm="", by=None, note="", hour=12):
    row = WorkOrderStatusHistory.objects.create(work_order=wo, from_status=frm, to_status=to, changed_by=by, note=note)
    WorkOrderStatusHistory.objects.filter(pk=row.pk).update(created_at=aware(on, hour))


def job(asset, *, opened, completed=None, vendor=VENDOR, type_=WoType.REPAIR, priority=Priority.NORMAL, assigned=(), by=None,
        recorded=None, source=Source.MANUAL, reference="", engineer=None, check=None, finding=None, status=None, due=None, late_reason=""):
    """A work order on vendor service under `vendor` ("" or None: in-house), opened on `opened` (its status row that morning);
    `assigned`: [(day, name)], an "Assigned to vendor: <name>" row each, mid-morning; completed on `completed` by `by`, its status row
    that afternoon (on `recorded` when given). A completed repair is checked and found worn out unless `check` / `finding` say."""
    repair = type_ == WoType.REPAIR
    done = completed is not None
    status = status or (WoStatus.COMPLETED if done else WoStatus.IN_PROGRESS)
    wo = WorkOrder.objects.create(asset=asset, type=type_, priority=priority, status=status, source=source, problem=SECRET, requester=SECRET,
                                  callback=SECRET, reported_location=SECRET, resolution=SECRET if done else "", vendor_service=bool(vendor),
                                  vendor_name=vendor or "", opened_on=opened, due_on=due or opened + 5 * DAY, completed_on=completed,
                                  checks_reference=reference, vendor_engineer=engineer, late_reason=late_reason,
                                  repair_check=(RepairCheck.PASSED if check is None else check) if repair and done else "",
                                  repair_finding=(RepairFinding.WEAR if finding is None else finding) if repair and done else "")
    status_row(wo, WoStatus.OPEN, on=opened, note="Opened", hour=8)
    for day, name in assigned:
        status_row(wo, WoStatus.OPEN, frm=WoStatus.OPEN, on=day, note=f"Assigned to vendor: {name}", hour=10)
    if done:
        status_row(wo, WoStatus.COMPLETED, frm=WoStatus.IN_PROGRESS, on=recorded or completed, by=by, hour=15)
    return wo


def saved(contract_id, day):
    """Date the contract's newest history row on `day`."""
    latest = Contract.history.filter(id=contract_id).order_by("-history_date", "-history_id").first()
    Contract.history.filter(history_id=latest.history_id).update(history_date=aware(day))


def contract(reference, vendor, start, end, cost, *, on, **fields):
    c = cs.create_contract(reference=reference, vendor=vendor, start_on=start, end_on=end, annual_cost=Decimal(cost), **fields)
    saved(c.pk, on)
    return c


def change(c, day, **fields):
    cs.update_contract(c, **fields)
    saved(c.pk, day)
    return c


def settings_on(day, **fields):
    """Change Settings and date the change's history row on `day`."""
    s = fac.update_settings(**fields)
    latest = FacilitySettings.history.order_by("-history_date", "-history_id").first()
    FacilitySettings.history.filter(history_id=latest.history_id).update(history_date=aware(day))
    return s


def take(span_, today, keys=(KEY,)):
    S, E = span_
    return vf.snapshot(list(keys), S, E, today=today)


def fig(data, key) -> dict:
    return vf.by_key(data)[key]


def split(figure, key) -> dict:
    return {s["key"]: s for s in figure["splits"]}[key]


def nd(f) -> tuple:
    return f["num"], f["den"]


def prorate(annual, days, period_days) -> float:
    return float((Decimal(annual) * days / period_days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def read_jobs(span_, today, keys=(KEY,)) -> dict:
    """{number: the module's _Job} with who and handoff read, for the attribution rules."""
    S, E = span_
    found = set(keys)
    qs, jobs = vf._jobs(vf._job_names(found), vf._Span(S, E, today))
    vf._status_rows(qs, jobs, found)
    return {j.number: j for j in jobs}


# --- the shape --------------------------------------------------------------------------------------------------------------------


def test_the_shape_with_no_vendor_work(ctx, today, span):
    data = take(span, today)
    json.dumps(data)  # JSON-ready
    assert data["schema"] == vf.SCHEMA == 1 and data["vendor_keys"] == [KEY] and data["taken_on"] == today.isoformat()
    assert data["period"] == {"start": span[0].isoformat(), "end": span[1].isoformat()}
    assert [f["key"] for f in data["figures"]] == list(vf.KEYS)
    assert {f["group"] for f in data["figures"]} == {g["key"] for g in data["groups"]} == {k for k, _l in vf.GROUPS}
    for f in data["figures"]:
        assert set(f) == {"key", "group", "label", "unit", "basis", "rule", "value", "num", "den", "display", "detail", "splits"}
        assert f["unit"] in (vf.PCT, vf.COUNT, vf.DAYS, vf.MONEY) and f["basis"] in (vf.PERIOD, vf.TAKEN) and f["rule"]
    assert fig(data, "time_median")["display"] == "No repairs" and fig(data, "pm_on_time")["display"] == "None due"
    assert fig(data, "doc_report_number")["display"] == "No jobs" and fig(data, "no_problem_found")["display"] == "Not recorded"
    assert fig(data, "cost_total")["display"] == "$0.00" and fig(data, "jobs_completed")["display"] == "0"
    assert fig(data, "repeat_repairs")["display"] == "No repairs" and fig(data, "repeat_repairs")["detail"] == ""
    assert data["contracts"] == [] and data["repeats"] == [] and data["names"] == [] and data["provisional_until"] is None
    assert data["settings"] == {"as_of": span[1].isoformat(), "pm_windows": {"high": "By the due date", "other": "By the due date"},
                                "pm_targets": {"high": 100.0, "other": 95.0}}
    assert data["not_recorded"] == list(vf.NOT_RECORDED) and len(data["not_recorded"]) == 3
    assert vf.snapshot(["", KEY, KEY], *span, today=today)["vendor_keys"] == [KEY]


def test_late_reasons_are_partitioned_by_side():
    sides = [*vf.VENDOR_REASONS, *vf.FACILITY_REASONS, *vf.OTHER_REASONS]
    assert sorted(map(str, sides)) == sorted(LateReason.values) and len(sides) == len(set(sides))


# --- contracts, from their history ------------------------------------------------------------------------------------------------


def test_contracts_come_from_their_history(ctx, today, span, dept, monitors, monitor_model):
    S, E = span
    days = (E - S).days + 1
    m = monitors
    c1 = contract("SC-1", "Philips", S - 100 * DAY, E + 200 * DAY, "36500.00", on=S - 100 * DAY)
    cs.add_asset(c1, m["m1"])
    cs.add_asset(c1, m["m2"])
    Asset.objects.create(tag="CE-20009", device_model=monitor_model, department=dept, status=AssetStatus.RETIRED, contract=c1)
    change(c1, S + 100 * DAY, annual_cost=Decimal("73000.00"))  # a price change partway: each price for its own days
    change(c1, S + 300 * DAY, notes="Renegotiated")  # nothing counted changed: still one version
    contract("SC-3", "Philips", S - 400 * DAY, S + 99 * DAY, "18250.00", on=S - 400 * DAY)  # ended in the period, never renewed
    c4 = contract("SC-4", "Philips field service", S, S + 199 * DAY, "36500.00", on=S - 10 * DAY)
    cs.add_asset(c4, m["m4"])
    c4_id = c4.pk
    cs.delete_contract(c4)
    saved(c4_id, S + 150 * DAY)  # deleted partway: its days before still count
    c5 = contract("SC-5", "GE Healthcare", S - 50 * DAY, E + 50 * DAY, "36500.00", on=S - 50 * DAY)
    cs.add_asset(c5, m["m3"])
    change(c5, S + 200 * DAY, vendor="Philips")  # renamed: the days before stay GE Healthcare's
    contract("SC-6", "PHILIPS", S - 30 * DAY, E + 300 * DAY, "3650.00", on=E + 10 * DAY)  # entered after the period: as first saved
    contract("SC-7", "Philips", S - 400 * DAY, S - DAY, "1000.00", on=S - 400 * DAY)  # ended before the period
    c8 = contract("SC-8", "Philips", S - 400 * DAY, S + 50 * DAY, "1000.00", on=S - 400 * DAY)
    change(c8, S + 10 * DAY, vendor="Siemens Healthineers")  # another vendor's from S+10: its first 10 days of the period stay Philips's

    data = take(span, today)
    contracts = {c["reference"]: c for c in data["contracts"]}
    assert [c["reference"] for c in data["contracts"]] == ["SC-1", "SC-3", "SC-4", "SC-5", "SC-6", "SC-8"]
    c1_data = contracts["SC-1"]
    assert [(v["from"], v["through"], v["days"], v["annual_cost"]) for v in c1_data["versions"]] == [
        (S.isoformat(), (S + 99 * DAY).isoformat(), 100, 36500.0), ((S + 100 * DAY).isoformat(), E.isoformat(), days - 100, 73000.0)]
    assert c1_data["cost"] == pytest.approx(prorate(36500, 100, days) + prorate(73000, days - 100, days))
    v = c1_data["versions"][0]
    assert (v["reference"], v["vendor"], v["type"], v["type_label"], v["coverage"], v["coverage_label"], v["start_on"], v["end_on"]) == (
        "SC-1", "Philips", "oem", "OEM", "full", "Full service", (S - 100 * DAY).isoformat(), (E + 200 * DAY).isoformat())
    assert (c1_data["devices_today"], c1_data["deleted"], c1_data["vendor_today"]) == (2, False, "Philips")  # the retired one is not
    assert (contracts["SC-3"]["days"], contracts["SC-3"]["cost"], contracts["SC-3"]["devices_today"]) == (100, prorate(18250, 100, days), 0)
    c4_data = contracts["SC-4"]
    assert (c4_data["days"], c4_data["cost"], c4_data["deleted"], c4_data["vendor_today"], c4_data["devices_today"]) == (
        150, prorate(36500, 150, days), True, None, None)
    c5_data = contracts["SC-5"]
    assert (c5_data["days"], c5_data["devices_today"], c5_data["versions"][0]["from"]) == (days - 200, 1, (S + 200 * DAY).isoformat())
    assert (contracts["SC-6"]["days"], contracts["SC-6"]["cost"]) == (days, 3650.0)
    assert (contracts["SC-8"]["days"], contracts["SC-8"]["vendor_today"], contracts["SC-8"]["devices_today"]) == (10, "Siemens Healthineers", None)
    total = sum(c["cost"] for c in data["contracts"])
    assert fig(data, "contracts_in_period")["value"] == 6
    assert fig(data, "contract_cost")["value"] == pytest.approx(total)
    assert fig(data, "contract_cost")["display"] == f"${Decimal(str(total)).quantize(Decimal('0.01')):,.2f}"
    assert fig(data, "devices_covered")["value"] == 3 and fig(data, "devices_covered")["basis"] == vf.TAKEN
    assert data["names"] == ["PHILIPS", "Philips", "Philips field service"]

    ge = take(span, today, keys=("ge healthcare",))
    assert [(c["reference"], c["days"], c["vendor_today"], c["devices_today"]) for c in ge["contracts"]] == [("SC-5", 200, "Philips", None)]
    assert fig(ge, "contract_cost")["value"] == prorate(36500, 200, days) and fig(ge, "devices_covered")["value"] == 0


def test_a_renewal_counts_the_days_its_new_term_adds(ctx, today, span):
    """Renewed in place: before the renewal the old end date stood, so a lapse between counts no days."""
    S, E = span
    days = (E - S).days + 1
    c = contract("SC-1", "Philips", S - 300 * DAY, S + 59 * DAY, "36500.00", on=S - 300 * DAY)
    cs.renew_contract(c, today=S + 90 * DAY)  # lapsed on S+59, renewed on S+90: twelve months from S+90
    saved(c.pk, S + 90 * DAY)
    data = take(span, today)
    (only,) = data["contracts"]
    assert [(v["from"], v["through"]) for v in only["versions"]] == [(S.isoformat(), (S + 59 * DAY).isoformat()),
                                                                     ((S + 90 * DAY).isoformat(), E.isoformat())]
    assert only["days"] == 60 + days - 90


# --- jobs, who completed them, and time with the vendor ---------------------------------------------------------------------------


@pytest.fixture
def jobs_world(ctx, span, monitors, vent, vendor_account, ce, dana):
    """J1-J3 the vendor's repairs, J4 its PM, J5 imported, and work that is not the vendor's or not in the period."""
    S, E = span
    m = monitors

    def d(n):
        return S + n * DAY

    w = {
        "J1": job(m["m1"], priority=Priority.HIGH, opened=d(10), assigned=[(d(12), "Philips")], completed=d(15), by=vendor_account,
                  reference="FSR-1", engineer=dana),
        "J2": job(m["m2"], vendor="Philips field service", opened=d(20), completed=d(30), by=ce, finding=RepairFinding.NO_PROBLEM_FOUND),
        "J3": job(m["m1"], opened=d(100), assigned=[(d(101), "GE Healthcare"), (d(105), "philips")], completed=d(140), by=ce, check="",
                  finding=""),
        "J4": job(m["m2"], type_=WoType.PM, opened=d(45), due=d(50), completed=d(50), by=vendor_account, reference="FSR-4", engineer=dana),
        "J5": job(m["m1"], source=Source.IMPORTED, opened=d(55), completed=d(60)),
        "J6": job(vent, vendor="Hamilton Medical field service", opened=d(10), completed=d(20), by=ce),
        "J7": job(m["m1"], opened=S - 20 * DAY, completed=S - DAY, by=ce),
        "J8": job(m["m2"], opened=E - 2 * DAY, completed=E + DAY, by=ce),
        "J9": job(m["m2"], opened=d(200)),
    }
    w["d"] = d
    return w


def test_jobs_by_type_and_by_who_imported_apart(jobs_world, today, span):
    data = take(span, today)
    f = fig(data, "jobs_completed")
    assert f["value"] == 4
    assert {s["key"]: s["value"] for s in f["splits"]} == {"pm": 1, "repair": 3, "inspection": 0, "recall": 0, "safety": 0,
                                                           "vendor": 2, "facility": 2}
    assert split(f, "vendor")["label"] == "Completed by the vendor's account" and split(f, "vendor")["detail"] == "2 of 4"
    assert fig(data, "jobs_imported")["value"] == 1
    assert data["names"] == ["Philips", "Philips field service"]


def test_time_with_the_vendor_and_device_downtime(jobs_world, today, span):
    """J1 3 days with the vendor (assigned day 12, done 15; high priority, the vendor's account), J2 10 (opened as vendor service), J3 35
    (assigned to GE first, then Philips on day 105). Downtime 5, 10, 40."""
    data = take(span, today)
    med, slow = fig(data, "time_median"), fig(data, "time_slowest")
    assert (med["value"], med["display"], med["detail"], med["den"], med["num"]) == (10.0, "10 days", "3 repairs", 3, None)
    assert (slow["value"], slow["display"]) == (35, "35 days")
    assert (fig(data, "time_within_7")["value"], nd(fig(data, "time_within_7"))) == (33.3, (1, 3))
    assert (fig(data, "time_within_30")["value"], nd(fig(data, "time_within_30"))) == (66.6, (2, 3))
    assert {s["key"]: s["value"] for s in med["splits"]} == {"urgent_high": 3.0, "normal_low": 22.5, "vendor": 3.0, "facility": 22.5}
    assert split(med, "normal_low")["display"] == "22.5 days" and split(med, "urgent_high")["display"] == "3 days"
    assert split(med, "urgent_high")["label"] == "Critical and high priority"
    assert {s["key"]: s["value"] for s in fig(data, "time_within_7")["splits"]} == {"urgent_high": 100.0, "normal_low": 0.0, "vendor": 100.0,
                                                                                    "facility": 0.0}
    assert (fig(data, "downtime_median")["value"], fig(data, "downtime_slowest")["value"]) == (10.0, 40)
    assert {s["key"]: s["value"] for s in fig(data, "downtime_median")["splits"]} == {"urgent_high": 5.0, "normal_low": 25.0, "vendor": 5.0,
                                                                                      "facility": 25.0}


def test_the_handoff_day_and_who_completed(ctx, today, span, monitors, vendor_account, ce, make_user):
    S, E = span
    m1 = monitors["m1"]

    def d(n):
        return S + n * DAY

    root = make_user("vendor", username="root@riverside.example")
    User.objects.filter(pk=root.pk).update(is_superuser=True)  # a vendor role, but a superuser: CE staff
    root.refresh_from_db()
    company = Role.objects.create(name="Field partner", slug="field-partner", scope=DataScope.COMPANY)
    partner = User.objects.create_user(username="partner@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=company,
                                       company=VENDOR)
    h1 = job(m1, priority=Priority.CRITICAL, opened=d(10), completed=d(14), by=vendor_account)  # opened as vendor service: from opened
    h2 = job(m1, opened=d(20), assigned=[(d(21), "GE Healthcare"), (d(25), "Philips field service")], completed=d(31), by=ce)
    h3 = job(m1, opened=d(40), assigned=[(d(41), "Philips"), (d(45), "PHILIPS")], completed=d(50))  # the last one; nobody recorded
    h4 = job(m1, opened=d(55), assigned=[(d(60), "Philips"), (d(70), "Philips")], completed=d(75), by=vendor_account)
    status_row(h4, WoStatus.COMPLETED, frm=WoStatus.IN_PROGRESS, on=d(62), by=ce, hour=15)  # completed once by CE, reopened
    status_row(h4, WoStatus.IN_PROGRESS, frm=WoStatus.COMPLETED, on=d(65), by=ce, hour=9)
    h6 = job(m1, opened=d(90), assigned=[(d(96), "Philips")], completed=d(95), recorded=d(97), by=root)  # completed as of an earlier day
    h7 = job(m1, opened=d(105), assigned=[(d(106), "Philips")], completed=d(110), by=ce)
    status_row(h7, WoStatus.OPEN, frm=WoStatus.OPEN, on=d(112), note="Assigned to vendor: Philips", hour=10)  # after the completion
    h8 = job(m1, opened=d(120), completed=d(123), by=partner)
    jobs = read_jobs(span, today)
    got = {wo.number: (jobs[wo.number].who, jobs[wo.number].handoff, jobs[wo.number].with_vendor)
           for wo in (h1, h2, h3, h4, h6, h7, h8)}
    assert got == {h1.number: ("vendor", d(10), 4), h2.number: ("facility", d(25), 6), h3.number: ("facility", d(45), 5),
                   h4.number: ("vendor", d(70), 5), h6.number: ("facility", d(95), 0), h7.number: ("facility", d(106), 4),
                   h8.number: ("vendor", d(120), 3)}
    data = take(span, today)
    med = fig(data, "time_median")
    assert (med["value"], fig(data, "time_slowest")["value"]) == (4.0, 6)
    assert (split(med, "vendor")["value"], split(med, "vendor")["den"], split(med, "facility")["value"], split(med, "facility")["den"]) == (
        4.0, 3, 4.5, 4)
    assert (split(med, "urgent_high")["value"], split(med, "urgent_high")["den"]) == (4.0, 1)


def test_the_status_rows_the_services_write(ctx, today, monitors, vendor_account, ce):
    """Assigned through workorders.services.assign and completed by the vendor's own account through complete_work_order: the note this
    module reads, the vendor's account, and the day (the facility's) of the handoff."""
    m1 = monitors["m1"]
    wo = create_work_order(asset=m1, type=WoType.REPAIR, priority=Priority.HIGH, problem=SECRET, opened_on=today - 3 * DAY)
    assign(wo, vendor_name="Philips field service", by=ce)
    assert WorkOrderStatusHistory.objects.filter(work_order=wo, note=f"{vf.ASSIGNED_NOTE}Philips field service").exists()
    change_status(wo, WoStatus.IN_PROGRESS, by=vendor_account, as_of=today)
    complete_work_order(wo, resolution="Replaced the power supply", by=vendor_account, today=today, **CHECKED)
    span_ = (today - 30 * DAY, today)
    (only,) = read_jobs(span_, today).values()
    assert (only.number, only.who, only.handoff, only.with_vendor, only.downtime) == (wo.number, "vendor", today, 0, 3)
    data = take(span_, today)
    assert split(fig(data, "jobs_completed"), "vendor")["value"] == 1 and fig(data, "time_median")["value"] == 0.0


# --- PMs --------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def pm_world(ctx, span, monitors, vent, ce):
    """The vendor's PMs P1 (on time), P2 (late, waiting on the vendor), P3 (open, no reason), P4 (late, device in use), P5 (late, other);
    Hamilton's late PM; on the contract's monitors, Q1 in-house done, Q2 in-house not done, Q3 another vendor's done; Q4 after the
    period."""
    S, E = span
    m1, m2 = monitors["m1"], monitors["m2"]
    c1 = contract("SC-1", "Philips", S - 100 * DAY, E + 200 * DAY, "12000.00", on=S - 100 * DAY)
    cs.add_asset(c1, m1)
    cs.add_asset(c1, m2)

    def d(n):
        return S + n * DAY

    def pm(asset, due, done=None, *, vendor=VENDOR, reason=""):
        return job(asset, type_=WoType.PM, opened=due - 20 * DAY, due=due, completed=done, vendor=vendor, late_reason=reason, by=ce)

    return {
        "P1": pm(m1, d(50), d(50)),
        "P2": pm(m2, d(80), d(90), vendor="Philips field service", reason=LateReason.WAITING_PARTS_VENDOR),
        "P3": pm(m1, d(120)),
        "P4": pm(m1, d(200), d(210), reason=LateReason.DEVICE_IN_USE),
        "P5": pm(m2, d(220), d(230), reason=LateReason.OTHER),
        "P6": pm(vent, d(100), d(130), vendor="Hamilton Medical field service"),
        "Q1": pm(m2, d(150), d(150), vendor=""),
        "Q2": pm(m1, d(250), vendor=""),
        "Q3": pm(m2, d(260), d(262), vendor="GE Healthcare"),
        "Q4": pm(m1, E + 10 * DAY, vendor=""),
    }


def test_the_vendors_pms_and_why_they_were_late(pm_world, today, span):
    data = take(span, today)
    on_time = fig(data, "pm_on_time")
    assert (on_time["value"], nd(on_time), on_time["display"], on_time["detail"]) == (20.0, (1, 5), "20%", "1 of 5")
    vendor_side = fig(data, "pm_late_vendor")
    assert nd(vendor_side) == (2, 4)
    assert [(s["key"], s["label"], s["value"]) for s in vendor_side["splits"]] == [
        ("waiting_parts_vendor", "Waiting on parts or the vendor", 1), ("none", "No reason recorded", 1)]
    facility_side = fig(data, "pm_late_facility")
    assert nd(facility_side) == (1, 4) and split(facility_side, "device_in_use")["value"] == 1
    assert {s["key"] for s in facility_side["splits"]} == {str(r) for r in vf.FACILITY_REASONS}
    assert nd(fig(data, "pm_late_other")) == (1, 4) and fig(data, "pm_pending")["value"] == 0
    covered = fig(data, "pm_covered_not_assigned")
    assert nd(covered) == (3, 8) and covered["detail"] == "3 of 8"
    assert {s["key"]: nd(s) for s in covered["splits"]} == {"completed": (2, 8), "not_completed": (1, 8)}


def test_the_pm_window_in_force_at_the_periods_end(pm_world, today, span):
    S, E = span
    both = {"pm_window_high": PmWindow.DAYS_AFTER, "pm_window_high_days": 14, "pm_window_other": PmWindow.DAYS_AFTER, "pm_window_other_days": 14}
    settings_on(S - 10 * DAY, **both)
    settings_on(E + 5 * DAY, pm_window_high=PmWindow.DUE_DATE, pm_window_high_days=None, pm_window_other=PmWindow.DUE_DATE,
                pm_window_other_days=None, target_pm_pct=Decimal("90.0"))  # after the period: not this review's
    data = take(span, today)
    assert nd(fig(data, "pm_on_time")) == (4, 5)  # P2, P4, P5 ten days late: inside 14
    assert nd(fig(data, "pm_late_vendor")) == (1, 1) and split(fig(data, "pm_late_vendor"), "none")["value"] == 1
    assert data["settings"]["pm_windows"] == {"high": "Within 14 days after the due date", "other": "Within 14 days after the due date"}
    assert data["settings"]["pm_targets"] == {"high": 100.0, "other": 95.0}


def test_pending_pms_and_provisional_figures(ctx, today, monitors):
    """A period that ended yesterday, under a month window: a PM due in it, not done, is still inside its window (not counted yet), and
    repeat repairs can still come for 30 days."""
    S, E = add_months(today - DAY, -12) + DAY, today - DAY
    settings_on(S - 10 * DAY, pm_window_high=PmWindow.NEXT_MONTH, pm_window_other=PmWindow.NEXT_MONTH)
    due = E - 2 * DAY
    job(monitors["m1"], type_=WoType.PM, opened=due - 20 * DAY, due=due)
    data = vf.snapshot([KEY], S, E, today=today)
    window_end = add_months(due.replace(day=1), 2) - DAY
    assert fig(data, "pm_pending")["value"] == 1 and fig(data, "pm_on_time")["den"] == 0
    assert data["provisional_until"] == max(window_end, E + 30 * DAY).isoformat()
    later = vf.snapshot([KEY], S, E, today=max(window_end, E + 30 * DAY) + DAY)
    assert later["provisional_until"] is None and fig(later, "pm_pending")["value"] == 0
    assert nd(fig(later, "pm_on_time")) == (0, 1) and split(fig(later, "pm_late_vendor"), "none")["value"] == 1


# --- repeat repairs ---------------------------------------------------------------------------------------------------------------


def test_repeat_repairs_count_a_failure_or_an_unknown_finding(ctx, today, span, monitors, vent, ce):
    S, E = span
    m = monitors

    def d(n):
        return S + n * DAY

    v1 = job(m["m1"], opened=d(10), completed=d(15), by=ce)
    r1 = job(m["m1"], vendor="", opened=d(25), completed=d(27), finding=RepairFinding.BATTERY)  # a failure: counted
    job(m["m2"], opened=d(25), completed=d(30), by=ce)  # v2
    job(m["m2"], vendor="", opened=d(40), completed=d(41), finding=RepairFinding.USE_ERROR)  # use error: not counted
    v3 = job(m["m1"], opened=d(100), completed=d(140), by=ce)
    r3 = job(m["m1"], vendor="", opened=d(160))  # still open: unknown, counted
    job(m["m1"], vendor="", opened=d(171), completed=d(172))  # 31 days after: not matched
    v4 = job(m["m3"], opened=d(195), completed=d(200), by=ce)
    v5 = job(m["m3"], opened=d(205), completed=d(210), by=ce)  # itself a repeat of v4 (worn out)
    r5 = job(m["m3"], vendor="", opened=d(220), completed=d(221), finding=RepairFinding.NOT_STATED)  # matched to v5, the last: unknown
    job(m["m3"], vendor="", opened=d(225), status=WoStatus.CANCELLED)  # cancelled: never
    v6 = job(m["m4"], opened=d(295), completed=d(300), by=ce)
    r7 = job(m["m4"], vendor="", opened=d(300), completed=d(302), finding=RepairFinding.SOFTWARE)  # the same day: counted
    job(m["m4"], source=Source.IMPORTED, opened=d(240), completed=d(250))  # imported: never a vendor repair here
    job(m["m4"], vendor="", opened=d(260), completed=d(261))  # after the imported one only: not matched
    job(vent, vendor="Hamilton Medical field service", opened=d(45), completed=d(50), by=ce)
    job(vent, vendor="", opened=d(60), completed=d(61))  # another vendor's repeat
    v9 = job(m["m5"], opened=E - 10 * DAY, completed=E - 5 * DAY, by=ce)
    r10 = job(m["m5"], vendor="", opened=E + 10 * DAY, completed=E + 11 * DAY)  # after the period, within its 30 days
    job(m["m2"], type_=WoType.PM, opened=d(60), due=d(65), completed=d(65), by=ce)  # a PM is never a vendor repair here
    m6 = Asset.objects.create(tag="CE-20006", device_model=m["m1"].device_model, department=m["m1"].department)
    job(m6, opened=E - 8 * DAY, completed=E - 6 * DAY, by=ce)  # v10
    job(m6, opened=E + DAY, completed=E + 3 * DAY, by=ce, finding=RepairFinding.ENVIRONMENT)  # the vendor's after the period: not counted
    job(m6, vendor="", opened=E + 12 * DAY, completed=E + 13 * DAY)  # after the vendor's repair after the period: never this period's

    data = take(span, today)
    f = fig(data, "repeat_repairs")
    assert (nd(f), f["detail"]) == ((6, 8), "6 of 8, 2 not recorded")
    assert nd(fig(data, "repeat_not_recorded")) == (2, 6) and fig(data, "repeat_other")["value"] == 2  # use error, and power or room
    assert [(p["repair"], p["later"], p["days"], p["finding"], p["finding_label"]) for p in data["repeats"]] == [
        (v1.number, r1.number, 10, "battery", "Battery failed"),
        (v3.number, r3.number, 20, "", "Still open"),
        (v4.number, v5.number, 5, "wear", "Wore out or failed in use"),
        (v5.number, r5.number, 10, "not_stated", "Not stated by the vendor"),
        (v6.number, r7.number, 0, "software", "Software or firmware fault"),
        (v9.number, r10.number, 15, "wear", "Wore out or failed in use"),
    ]
    assert data["repeats"][0] == {"repair": v1.number, "completed_on": d(15).isoformat(), "later": r1.number, "opened_on": d(25).isoformat(),
                                  "days": 10, "finding": "battery", "finding_label": "Battery failed"}
    assert data["provisional_until"] is None  # the period ended 40 days ago

    recent = (add_months(today - DAY, -12) + DAY, today - DAY)
    assert take(recent, today)["provisional_until"] == (today + 29 * DAY).isoformat()  # the period's end + 30 days


# --- findings, documentation, staff evidence, cost --------------------------------------------------------------------------------


def test_findings_documentation_staff_evidence_and_cost(jobs_world, today, span, monitor_model, dana):
    S, E = span
    d = jobs_world["d"]
    t = vs.add_training([dana], covers=Covers.MODELS, model_ids=[str(monitor_model.pk)], source=TrainingSource.OEM,
                        issued_on=S - 200 * DAY, expires_on=None)[0]
    type(t).objects.filter(pk=t.pk).update(created_at=aware(S - 200 * DAY))  # on file before the work
    vs.add_attestation(vendor_names=["Philips"], covers=Covers.MODELS, model_ids=[str(monitor_model.pk)], reference="LTR-1",
                       received_on=d(25), valid_from=d(25), valid_through=d(35))  # entered today: after J2's day
    j1, j4, j5, j6 = jobs_world["J1"], jobs_world["J4"], jobs_world["J5"], jobs_world["J6"]
    LaborLine.objects.create(work_order=j1, worked_on=d(15), hours=Decimal("2"), rate=Decimal("150"))
    PartLine.objects.create(work_order=j1, description="Power supply", quantity=Decimal("1"), unit_cost=Decimal("45.50"))
    LaborLine.objects.create(work_order=j4, worked_on=d(50), hours=Decimal("1.5"), rate=Decimal("100"))
    LaborLine.objects.create(work_order=j5, worked_on=d(60), hours=Decimal("1"), rate=Decimal("80"))  # imported: apart
    LaborLine.objects.create(work_order=j6, worked_on=d(20), hours=Decimal("5"), rate=Decimal("100"))  # another vendor's
    data = take(span, today)

    npf = fig(data, "no_problem_found")
    assert (npf["value"], nd(npf), npf["group"]) == (50.0, (1, 2), "use")

    def shares(key):
        f = fig(data, key)
        return nd(f), {s["key"]: nd(s) for s in f["splits"]}

    assert shares("doc_report_number") == ((2, 4), {"vendor": (2, 2), "facility": (0, 2)})
    assert shares("doc_engineer") == ((2, 4), {"vendor": (2, 2), "facility": (0, 2)})
    assert shares("doc_checks") == ((2, 3), {"vendor": (1, 1), "facility": (1, 2)})
    assert shares("doc_finding") == ((2, 3), {"vendor": (1, 1), "facility": (1, 2)})
    assert fig(data, "doc_checks")["display"] == "66.6%"
    assert nd(fig(data, "staff_training")) == (2, 4) and nd(fig(data, "staff_attestation")) == (1, 4)
    assert nd(fig(data, "staff_nothing")) == (1, 4) and nd(fig(data, "staff_entered_after")) == (1, 3)
    money = {k: (fig(data, k)["value"], fig(data, k)["display"]) for k in ("cost_labor", "cost_parts", "cost_total", "cost_imported")}
    assert money == {"cost_labor": (450.0, "$450.00"), "cost_parts": (45.5, "$45.50"), "cost_total": (495.5, "$495.50"),
                     "cost_imported": (80.0, "$80.00")}
    assert fig(data, "cost_total")["den"] is None and fig(data, "cost_total")["detail"] == ""


# --- one vendor under two names ---------------------------------------------------------------------------------------------------


def test_two_names_are_one_vendor_only_when_the_review_names_both(ctx, today, span, monitors, ce):
    S, E = span
    contract("SC-9", "Philips Healthcare", S - 10 * DAY, E + 10 * DAY, "1000.00", on=S - 10 * DAY)
    job(monitors["m1"], vendor="Philips field service", opened=S + 20 * DAY, completed=S + 30 * DAY, by=ce)
    both = take(span, today, keys=("philips healthcare", "philips"))
    assert (fig(both, "contracts_in_period")["value"], fig(both, "jobs_completed")["value"]) == (1, 1)
    assert both["names"] == ["Philips field service", "Philips Healthcare"] and both["vendor_keys"] == ["philips healthcare", "philips"]
    contract_only = take(span, today, keys=("philips healthcare",))
    assert (fig(contract_only, "contracts_in_period")["value"], fig(contract_only, "jobs_completed")["value"]) == (1, 0)
    jobs_only = take(span, today)
    assert (fig(jobs_only, "contracts_in_period")["value"], fig(jobs_only, "jobs_completed")["value"]) == (0, 1)


# --- what it never shows, its cost, and another facility ---------------------------------------------------------------------------


def test_nothing_typed_no_person_no_tag_and_never_uptime_or_response(jobs_world, today, span, monitors, vendor_account, ce, dana):
    S, E = span
    contract("SC-1", "Philips", S - 10 * DAY, E + 10 * DAY, "1000.00", on=S - 10 * DAY, notes=SECRET)
    data = take(span, today)
    text = json.dumps(data)
    for never in (SECRET, "Dana Kim", str(vendor_account), str(ce), *(a.tag for a in monitors.values()), "CE-10001"):
        assert never not in text, never
    for f in data["figures"]:
        words = " ".join([f["label"], f["display"], *(s["label"] for s in f["splits"])]).lower()
        assert "uptime" not in words and "response" not in words and "met" not in words.split()


def _scaled(first, n, span_, monitors, users, engineer):
    """`n` more vendor repairs (labor and parts, a repeat after each), vendor PMs, and contracts with a price change, numbered from
    `first` (each a week after the last, so no two repairs of a device fall within 30 days of each other)."""
    S, E = span_
    assets = list(monitors.values())
    for k in range(first, first + n):
        a = assets[k % len(assets)]
        start = S + (k * 7 % 280) * DAY
        v = job(a, priority=Priority.HIGH if k % 2 else Priority.NORMAL, opened=start, assigned=[(start + DAY, "Philips")],
                completed=start + 3 * DAY, by=users[k % 2], reference=f"FSR-{k}", engineer=engineer if k % 3 else None)
        LaborLine.objects.create(work_order=v, worked_on=start, hours=Decimal("1"), rate=Decimal("100"))
        PartLine.objects.create(work_order=v, description="Part", quantity=Decimal("1"), unit_cost=Decimal("5"))
        job(a, vendor="", opened=start + 5 * DAY, completed=start + 6 * DAY, finding=RepairFinding.BATTERY)
        job(a, type_=WoType.PM, opened=start, due=start + 10 * DAY, completed=start + (10 + k % 3) * DAY, by=users[k % 2],
            late_reason=LateReason.WAITING_PARTS_VENDOR if k % 3 else "")
        c = contract(f"SC-{k}", "Philips" if k % 2 else "Philips field service", S - 30 * DAY, E + 30 * DAY, "1200.00",
                     on=S - 30 * DAY)
        change(c, S + (k % 100) * DAY, annual_cost=Decimal("1300.00"))
        cs.add_asset(c, a)


def test_a_fixed_number_of_queries(ctx, today, span, monitors, monitor_model, vendor_account, ce, dana):
    S, E = span
    settings_on(S - 10 * DAY, pm_window_high=PmWindow.DUE_MONTH, pm_window_other=PmWindow.DUE_MONTH)  # a window: the pending query runs
    vs.add_training([dana], covers=Covers.MODELS, model_ids=[str(monitor_model.pk)], source=TrainingSource.OEM, issued_on=S - 10 * DAY)
    vs.add_attestation(vendor_names=["Philips"], covers=Covers.ALL, reference="LTR-1", received_on=S, valid_from=S, valid_through=E)
    _scaled(0, 2, span, monitors, [vendor_account, ce], dana)
    with CaptureQueriesContext(connection) as small:
        take(span, today)
    _scaled(2, 12, span, monitors, [vendor_account, ce], dana)
    with CaptureQueriesContext(connection) as large:
        data = take(span, today)
    assert fig(data, "jobs_completed")["value"] == 2 * 14 and fig(data, "contracts_in_period")["value"] == 14
    assert fig(data, "repeat_repairs")["num"] == 14
    assert len(small) == len(large) <= 17, [q["sql"][:120] for q in large.captured_queries]


def test_another_facility_is_never_read(ctx, today, span, monitors, ce, other_tenant):
    S, E = span
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                           risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=Department.objects.create(name="ER"))
        contract("SC-OT", "Philips", S - 10 * DAY, E + 10 * DAY, "5000.00", on=S - 10 * DAY)
        job(theirs, opened=S + 10 * DAY, completed=S + 12 * DAY)
        job(theirs, vendor="", opened=S + 14 * DAY, completed=S + 15 * DAY)
        job(theirs, type_=WoType.PM, opened=S, due=S + 20 * DAY, completed=S + 25 * DAY)
        settings_on(S - 10 * DAY, target_pm_pct=Decimal("80.0"))
        assert fig(take(span, today), "jobs_completed")["value"] == 2
    job(monitors["m1"], opened=S + 40 * DAY, completed=S + 41 * DAY, by=ce)
    data = take(span, today)
    assert fig(data, "jobs_completed")["value"] == 1 and data["contracts"] == [] and data["repeats"] == []
    assert fig(data, "pm_on_time")["den"] == 0 and data["settings"]["pm_targets"]["other"] == 95.0


# --- the cut ----------------------------------------------------------------------------------------------------------------------


def custom_user(tenant, name, levels: dict, scope: str = "") -> User:
    role = Role.objects.create(name=name, slug=name.lower().replace(" ", "-"), scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{role.slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    company=VENDOR if scope else "")


def test_the_cut_is_all_or_nothing(jobs_world, today, span, ctx, make_user):
    data = take(span, today)
    every = {m: Level.VIEW for m in vf.NEEDS}
    for slug in ("director", "manager", "analyst"):
        user = make_user(slug, username=f"reader-{slug}@riverside.example")
        seen = vf.cut(data, user)
        assert vf.can_see(user) and seen["figures"] == data["figures"] and seen["left_out"] == []
    assert vf.cut(data, custom_user(ctx, "Reviewer", every))["figures"] == data["figures"]
    refused = [*(make_user(slug, username=f"reader-{slug}@riverside.example") for slug in ("technician", "vendor", "requester")),
               custom_user(ctx, "Partner", every, DataScope.COMPANY),
               *(custom_user(ctx, f"Without {m}", {k: v for k, v in every.items() if k != m}) for m in vf.NEEDS)]
    for user in refused:
        seen = vf.cut(data, user)
        assert not vf.can_see(user)
        assert {k: seen[k] for k in vf.EMPTIED} == vf.EMPTIED
        assert seen["left_out"] == ["Every figure (needs Evaluations View, Contracts View, Reports View, and Work orders View)"]
        assert (seen["schema"], seen["vendor_keys"], seen["period"], seen["taken_on"]) == (1, [KEY], data["period"], data["taken_on"])
    assert vf.cut(None, None)["figures"] == [] and not vf.can_see(None)
    assert data["figures"] and "left_out" not in data  # the stored snapshot is never changed


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_figures_under_the_policies(jobs_world, today, span, other_tenant):
    S, E = span
    contract("SC-1", "Philips", S - 10 * DAY, E + 10 * DAY, "1000.00", on=S - 10 * DAY)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                           risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=Department.objects.create(name="ER"))
        contract("SC-OT", "Philips", S - 10 * DAY, E + 10 * DAY, "5000.00", on=S - 10 * DAY)
        job(theirs, opened=S + 10 * DAY, completed=S + 12 * DAY)
    before = take(span, today)
    as_app_role()
    after = take(span, today)
    assert after == before
    assert fig(after, "jobs_completed")["value"] == 4 and [c["reference"] for c in after["contracts"]] == ["SC-1"]
    assert split(fig(after, "jobs_completed"), "vendor")["value"] == 2  # the completing accounts' roles read under the policies
