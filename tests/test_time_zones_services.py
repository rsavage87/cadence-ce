"""
The services work on the facility's day (slice 21, part C): every model property and service that asks for today reads
timezone.localdate(), which inside a facility is its own day (its time zone, Tenant.timezone, is active there). The clock is pinned
at 08:30 UTC on Oct 5: still 22:30 on Oct 4 in a Honolulu facility, already 04:30 on Oct 5 in a New York one. The same records are
read in each zone, so the only difference is the facility's time zone.
"""
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from apps.contracts.models import Contract
from apps.contracts.services import contract_status
from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import credential_state, qualification
from apps.equipment.models import Asset
from apps.equipment.services import FleetBucket, fleet_bucket_counts
from apps.pm import aem
from apps.pm.services import generate_pm_work_orders, overdue_assets
from apps.recalls import feeds
from apps.reports.services import run_report
from apps.tenants.context import tenant_context
from apps.workorders.models import DUE_DAYS, Priority, WorkOrder, WoType
from apps.workorders.services import create_work_order

NOW = datetime(2026, 10, 5, 8, 30, tzinfo=dt_timezone.utc)
HONOLULU, NEW_YORK = "Pacific/Honolulu", "America/New_York"
OCT_4, OCT_5 = date(2026, 10, 4), date(2026, 10, 5)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: NOW)


def inside(tenant, zone):
    """Work inside `tenant` as if its time zone were `zone` (tenant_context activates the zone the facility has on entry)."""
    tenant.timezone = zone
    return tenant_context(tenant)


def test_the_two_facilities_are_on_different_days(tenant):
    with inside(tenant, HONOLULU):
        assert timezone.localdate() == OCT_4
    with inside(tenant, NEW_YORK):
        assert timezone.localdate() == OCT_5


# --- model properties ------------------------------------------------------------------------------------------------------------

def test_a_work_order_due_yesterday_in_new_york_is_due_today_in_honolulu(tenant, vent):
    with inside(tenant, HONOLULU):
        wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", due_on=OCT_4)
        assert wo.opened_on == OCT_4  # opened on the facility's today
        assert not wo.is_late
    with inside(tenant, NEW_YORK):
        assert wo.is_late


def test_a_work_order_opened_without_a_date_takes_the_facilitys_day(tenant, vent):
    with inside(tenant, HONOLULU):
        wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="No power")
        assert (wo.opened_on, wo.due_on) == (OCT_4, OCT_4 + timedelta(days=DUE_DAYS[Priority.HIGH]))
        assert WorkOrder(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="x", due_on=OCT_5).opened_on == OCT_4
    with inside(tenant, NEW_YORK):
        assert create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="No power").opened_on == OCT_5


def test_a_contract_ending_on_oct_4_has_ended_only_in_new_york(tenant, vent):
    with inside(tenant, HONOLULU):
        contract = Contract.objects.create(reference="SC-1", vendor="Hamilton", start_on=date(2025, 10, 5), end_on=OCT_4)
        vent.contract = contract
        vent.save()
        assert (contract.is_expired, contract.days_to_end, contract.status) == (False, 0, "ending")
        assert contract_status(contract)["key"] != "expired"
        assert vent.under_contract
    with inside(tenant, NEW_YORK):
        assert (contract.is_expired, contract.days_to_end, contract.status) == (True, -1, "expired")
        assert contract_status(contract)["key"] == "expired"
        assert not vent.under_contract


def test_a_devices_pm_days_and_age_count_from_the_facilitys_day(tenant, vent):
    with inside(tenant, HONOLULU):
        Asset.objects.filter(pk=vent.pk).update(next_pm_on=OCT_4, installed_on=date(2025, 10, 4))
        vent.refresh_from_db()
        assert vent.pm_days_remaining() == 0
        assert vent.age_years() == 365 / 365.25
        assert not overdue_assets().exists()
        assert fleet_bucket_counts()[FleetBucket.PM_DUE] == 1
    with inside(tenant, NEW_YORK):
        assert vent.pm_days_remaining() == -1
        assert vent.age_years() == 366 / 365.25
        assert list(overdue_assets()) == [vent]
        assert fleet_bucket_counts()[FleetBucket.PM_OVERDUE] == 1


def test_a_credential_expiring_on_oct_4_still_counts_in_honolulu(tenant, vent):
    with inside(tenant, HONOLULU):
        dana = Technician.objects.create(name="Dana Whitfield")
        cred = Credential.objects.create(technician=dana, scope=Scope.MODEL, value=vent.device_model.model, expires_on=OCT_4)
        assert qualification(dana, vent).ok
        assert credential_state(cred)["key"] != "expired"
    with inside(tenant, NEW_YORK):
        assert not qualification(dana, vent).ok
        assert credential_state(cred)["key"] == "expired"


# --- services that look ahead or back from today ---------------------------------------------------------------------------------

def test_pm_generation_looks_ahead_from_the_facilitys_day(tenant, vent, pump):
    with inside(tenant, HONOLULU):
        Asset.objects.filter(pk=vent.pk).update(next_pm_on=OCT_4)
        Asset.objects.filter(pk=pump.pk).update(next_pm_on=OCT_5)
        assert generate_pm_work_orders(lead_days=0) == 1  # Oct 5 is tomorrow in Honolulu
        wo = WorkOrder.objects.get(type=WoType.PM)
        assert (wo.asset_id, wo.opened_on, wo.due_on) == (vent.pk, OCT_4, OCT_4)
    with inside(tenant, NEW_YORK):
        assert generate_pm_work_orders(lead_days=0) == 1  # the pump's Oct 5 is today in New York
        assert WorkOrder.objects.get(type=WoType.PM, asset=pump).opened_on == OCT_5


def test_aem_evidence_runs_to_the_facilitys_day(tenant, vent):
    with inside(tenant, HONOLULU):
        Asset.objects.filter(pk=vent.pk).update(installed_on=OCT_5)  # installed "tomorrow" in Honolulu
        ev = aem.evidence(vent.device_model)
        assert (ev["as_of"], ev["since"], ev["devices_active"]) == ("2026-10-04", "2023-10-04", 0)
    with inside(tenant, NEW_YORK):
        ev = aem.evidence(vent.device_model)
        assert (ev["as_of"], ev["since"], ev["devices_active"]) == ("2026-10-05", "2023-10-05", 1)


def test_reports_run_as_of_the_facilitys_day(tenant, vent):
    with inside(tenant, HONOLULU):
        assert run_report("compliance")["today"] == OCT_4
    with inside(tenant, NEW_YORK):
        assert run_report("compliance")["today"] == OCT_5


def test_the_fda_checks_shared_row_is_for_the_servers_day_whoever_checks(tenant, settings):
    """The feed check's rows are everyone's, one a day: a Honolulu check at 22:30 on Oct 4 lands on the server's Oct 5 row, as a
    New York check at the same moment does, so the two never claim different rows."""
    settings.TIME_ZONE = NEW_YORK
    with inside(tenant, HONOLULU):
        assert feeds._row_day(NOW) == OCT_5


# --- signed-out and API paths ------------------------------------------------------------------------------------------------------

def test_a_portal_request_is_dated_on_the_facilitys_day(client, tenant, vent):
    tenant.timezone = HONOLULU
    tenant.save(update_fields=["timezone"])
    r = client.post(f"/r/{tenant.slug}/", {"asset_tag": vent.tag, "department": str(vent.department_id), "problem": "Alarm will not silence",
                                          "urgency": "normal", "requester_name": "Unit staff", "callback": "4410"})
    assert r.status_code == 302, r.content.decode()[:300]
    with tenant_context(tenant):
        wo = WorkOrder.objects.get(asset=vent)
    assert (wo.opened_on, wo.due_on) == (OCT_4, OCT_4 + timedelta(days=DUE_DAYS[Priority.NORMAL]))


def test_the_apis_pm_calendar_marks_the_facilitys_today(client, tenant, make_user):
    tenant.timezone = HONOLULU
    tenant.save(update_fields=["timezone"])
    client.force_login(make_user("director"))
    r = client.get("/api/v1/pm/calendar/?y=2026&m=10")
    assert r.status_code == 200
    today = [c["date"] for week in r.json()["weeks"] for c in week if c["is_today"]]
    assert today == ["2026-10-04"]
