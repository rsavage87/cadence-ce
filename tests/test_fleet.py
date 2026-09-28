from datetime import date, timedelta

from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, AssetStatus
from apps.equipment.services import AssetFilters, FleetBucket, asset_service_summary, filter_assets, fleet_bucket_counts, search_assets
from apps.workorders.models import LaborLine
from apps.workorders.services import change_status, create_work_order

TODAY = date.today()


def _asset(tag, model, dept, status=AssetStatus.IN_SERVICE, next_pm_days=90, **kw):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, status=status, next_pm_on=TODAY + timedelta(days=next_pm_days), **kw)


def _buckets():
    return {a.tag: a.bucket for a in filter_assets(AssetFilters())}


def test_each_device_lands_in_its_most_urgent_bucket(ctx, dept, vent_model, pump_model, pump_recall):
    _asset("RET", vent_model, dept, status=AssetStatus.RETIRED, next_pm_days=-50)
    _asset("OOS", vent_model, dept, status=AssetStatus.OUT_OF_SERVICE, next_pm_days=-50)
    _asset("REP", vent_model, dept, status=AssetStatus.IN_REPAIR, next_pm_days=-50)
    _asset("RCL", pump_model, dept, next_pm_days=-50)  # overdue too, but the open recall ranks first
    _asset("OVD", vent_model, dept, next_pm_days=-1)
    _asset("DUE", vent_model, dept, next_pm_days=30)
    _asset("OK", vent_model, dept, next_pm_days=31)
    assert _buckets() == {"RET": FleetBucket.RETIRED, "OOS": FleetBucket.OUT_OF_SERVICE, "REP": FleetBucket.IN_REPAIR, "RCL": FleetBucket.OPEN_RECALL,
                          "OVD": FleetBucket.PM_OVERDUE, "DUE": FleetBucket.PM_DUE, "OK": FleetBucket.COMPLIANT}
    counts = fleet_bucket_counts()
    assert sum(counts.values()) == 7 and all(n == 1 for n in counts.values())


def test_closed_recall_no_longer_puts_devices_in_the_recall_bucket(ctx, dept, pump_model, pump_recall):
    _asset("P1", pump_model, dept)
    pump_recall.status = pump_recall.Status.CLOSED
    pump_recall.save()
    assert _buckets()["P1"] == FleetBucket.COMPLIANT


def test_bucket_filter_matches_the_counts(ctx, dept, vent_model):
    _asset("A", vent_model, dept, next_pm_days=-3)
    _asset("B", vent_model, dept, next_pm_days=-9)
    _asset("C", vent_model, dept, next_pm_days=200)
    overdue = filter_assets(AssetFilters(bucket=FleetBucket.PM_OVERDUE))
    assert {a.tag for a in overdue} == {"A", "B"} == {a.tag for a in overdue if a.bucket == FleetBucket.PM_OVERDUE}
    assert fleet_bucket_counts()[FleetBucket.PM_OVERDUE] == 2


def test_support_filters_distinguish_active_and_expired_contracts(ctx, dept, vent_model):
    live = Contract.objects.create(reference="LIVE", vendor="Hamilton", type=ContractType.OEM, start_on=TODAY - timedelta(days=100),
                                   end_on=TODAY + timedelta(days=100))
    dead = Contract.objects.create(reference="DEAD", vendor="TechCare", type=ContractType.THIRD_PARTY, start_on=TODAY - timedelta(days=400),
                                   end_on=TODAY - timedelta(days=1))
    _asset("L", vent_model, dept, contract=live)
    _asset("D", vent_model, dept, contract=dead)
    _asset("H", vent_model, dept)

    def tags(support):
        return {a.tag for a in filter_assets(AssetFilters(support=support))}

    assert tags("under_contract") == {"L"}
    assert tags("contract_expired") == {"D"}
    assert tags("in_house") == {"H"}
    assert tags("oem_contract") == {"L"} and tags("third_party") == {"D"}


def test_overdue_only_skips_retired_devices(ctx, dept, vent_model):
    _asset("LATE", vent_model, dept, next_pm_days=-5)
    _asset("GONE", vent_model, dept, status=AssetStatus.RETIRED, next_pm_days=-5)
    assert [a.tag for a in filter_assets(AssetFilters(overdue=True))] == ["LATE"]


def test_search_and_sort(ctx, dept, vent_model, pump_model):
    _asset("CE-2", pump_model, dept, next_pm_days=5, serial="SN-XYZ")
    _asset("CE-1", vent_model, dept, next_pm_days=50)
    Asset.objects.create(tag="CE-3", device_model=vent_model, department=dept)  # no PM scheduled
    assert [a.tag for a in filter_assets(AssetFilters(q="xyz"))] == ["CE-2"]
    assert [a.tag for a in filter_assets(AssetFilters(q="hamilton"))] == ["CE-1", "CE-3"]
    assert [a.tag for a in filter_assets(AssetFilters(sort="next_pm"))] == ["CE-2", "CE-1", "CE-3"]  # unscheduled last
    assert [a.tag for a in filter_assets(AssetFilters(sort="next_pm", descending=True))] == ["CE-1", "CE-2", "CE-3"]  # still last
    assert [a.tag for a in filter_assets(AssetFilters(sort="risk"))] == ["CE-1", "CE-3", "CE-2"]  # life support, then high; tag breaks ties


def test_device_picker_needs_two_characters_and_skips_retired(ctx, dept, vent_model):
    _asset("CE-500", vent_model, dept)
    _asset("CE-501", vent_model, dept, status=AssetStatus.RETIRED)
    assert list(search_assets("C")) == []
    assert [a.tag for a in search_assets("ce-50")] == ["CE-500"]


def test_service_summary_counts_completed_cost_in_trailing_six_months(ctx, vent):
    old = create_work_order(asset=vent, type="repair", priority="normal", problem="Old", opened_on=TODAY - timedelta(days=200))
    LaborLine.objects.create(work_order=old, hours=10, rate=100)
    done = create_work_order(asset=vent, type="repair", priority="normal", problem="Done", opened_on=TODAY - timedelta(days=10))
    LaborLine.objects.create(work_order=done, hours=2, rate=100)
    change_status(done, "in_progress")
    change_status(done, "completed")
    open_ = create_work_order(asset=vent, type="pm", priority="normal", problem="Open PM")
    LaborLine.objects.create(work_order=open_, hours=1, rate=100)
    s = asset_service_summary(vent)
    assert [w.number for w in s["work_orders"]] == [open_.number, done.number]
    assert s["repairs"] == 1 and s["cost"] == 200
    assert round(s["annualized_pct"], 3) == round(200 * 365 / 182 / 38000 * 100, 3)


def test_asset_tags_cannot_carry_spaces_or_slashes(client, make_user, dept, vent_model):
    """Tags live in URLs (/equipment/<tag>/); a slash would make every screen that links the device fail to render."""
    import pytest
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="spaces or slashes"):
        Asset(tag="CE 10/5", device_model=vent_model, department=dept).full_clean(exclude=["tenant"])
    Asset(tag="CE-10005", device_model=vent_model, department=dept).full_clean(exclude=["tenant"])
    client.force_login(make_user("director"))
    r = client.post("/api/v1/assets/", {"tag": "CE/9", "device_model": vent_model.pk, "department": dept.pk}, content_type="application/json")
    assert r.status_code == 400 and "slashes" in r.json()["tag"][0]
