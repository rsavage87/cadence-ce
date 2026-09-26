from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType, Coverage
from apps.equipment.models import Asset, AssetStatus, SupportType
from apps.pm.dates import add_months

TODAY = date.today()


def test_adding_devices_sets_support_type(ctx, vent, pump):
    c = Contract.objects.create(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, start_on=date.today(),
                                end_on=date.today() + timedelta(days=365), annual_cost=5000)
    assert c.add_assets([vent]) == 1
    vent.refresh_from_db()
    assert vent.support_type == SupportType.OEM_CONTRACT and vent.under_contract
    c.remove_asset(vent)
    vent.refresh_from_db()
    assert vent.support_type == SupportType.IN_HOUSE and vent.contract is None


def test_device_moves_between_contracts(ctx, pump):
    a = Contract.objects.create(reference="A", vendor="V", type=ContractType.THIRD_PARTY, start_on=date.today(), end_on=date.today() + timedelta(days=30))
    b = Contract.objects.create(reference="B", vendor="V", type=ContractType.OEM, start_on=date.today(), end_on=date.today() + timedelta(days=30))
    a.add_assets([pump])
    b.add_assets([pump])
    pump.refresh_from_db()
    assert pump.contract == b and a.covered_assets().count() == 0 and pump.support_type == SupportType.OEM_CONTRACT


def test_expired_contract_is_not_under_contract(ctx, vent):
    c = Contract.objects.create(reference="OLD", vendor="V", start_on=date.today() - timedelta(days=400), end_on=date.today() - timedelta(days=1),
                                annual_cost=1000)
    c.add_assets([vent])
    vent.refresh_from_db()
    assert vent.support_type == SupportType.OEM_CONTRACT and not vent.under_contract and c.status == "expired" and c.cost_share_for(vent) == 0


def test_cost_share_allocates_by_acquisition_cost(ctx, vent, pump):
    c = Contract.objects.create(reference="S", vendor="V", start_on=date.today(), end_on=date.today() + timedelta(days=200), annual_cost=4120)
    c.add_assets([vent, pump])
    assert round(c.cost_share_for(vent)) == 3800 and round(c.cost_share_for(pump)) == 320
    assert Asset.objects.filter(contract=c).count() == 2


# --- services (slice 5) ---------------------------------------------------------------------------------

def make_contract(reference="SC-1", vendor="Hamilton Medical", start=None, end=None, **kw):
    return ct.create_contract(reference=reference, vendor=vendor, start_on=start or TODAY - timedelta(days=100),
                              end_on=end or TODAY + timedelta(days=265), **kw)


def test_create_validates_required_fields_and_dates(ctx):
    with pytest.raises(ValidationError) as e:
        ct.create_contract(reference=" ", vendor="", start_on=TODAY, end_on=TODAY - timedelta(days=1), annual_cost=-5)
    assert set(e.value.message_dict) == {"reference", "vendor", "end_on", "annual_cost"}
    c = make_contract(reference=" SC-9 ", vendor=" V ", annual_cost=0)
    assert (c.reference, c.vendor) == ("SC-9", "V") and c.end_on >= c.start_on


def test_duplicate_reference_is_friendly_and_case_insensitive(ctx):
    make_contract(reference="SC-1")
    with pytest.raises(ValidationError, match="already used"):
        make_contract(reference="sc-1")
    other = make_contract(reference="SC-2")
    with pytest.raises(ValidationError, match="already used"):
        ct.update_contract(other, reference="SC-1")
    ct.update_contract(other, reference="SC-2", notes=" keep ")  # its own reference is not a duplicate
    assert other.notes == "keep"


def test_update_rejects_unknown_fields_and_resyncs_support_type(ctx, vent):
    c = make_contract(type=ContractType.OEM)
    ct.add_asset(c, vent)
    with pytest.raises(ValidationError):
        ct.update_contract(c, tenant=None)
    ct.update_contract(c, type=ContractType.THIRD_PARTY)
    vent.refresh_from_db()
    assert vent.support_type == SupportType.THIRD_PARTY


def test_renew_extends_from_the_end_or_from_today(ctx):
    active = make_contract(reference="A", end=TODAY + timedelta(days=200))
    ct.renew_contract(active)
    assert active.end_on == add_months(TODAY + timedelta(days=200), 12)
    expired = make_contract(reference="E", start=TODAY - timedelta(days=500), end=TODAY - timedelta(days=30))
    ct.renew_contract(expired)
    assert expired.end_on == add_months(TODAY, 12) and expired.start_on == TODAY - timedelta(days=500)
    future = make_contract(reference="F", start=TODAY + timedelta(days=20), end=TODAY + timedelta(days=385))
    ct.renew_contract(future)
    assert future.start_on == TODAY and future.end_on == add_months(TODAY + timedelta(days=385), 12)


def test_delete_detaches_devices_and_reports_how_many(ctx, vent, pump):
    c = make_contract()
    ct.add_asset(c, vent)
    ct.add_asset(c, pump)
    assert ct.delete_contract(c) == 2
    vent.refresh_from_db()
    assert vent.contract is None and vent.support_type == SupportType.IN_HOUSE and not Contract.objects.exists()


def test_add_asset_moves_between_contracts_and_returns_the_previous_one(ctx, pump):
    a, b = make_contract(reference="A"), make_contract(reference="B")
    assert ct.add_asset(a, pump) is None
    assert ct.add_asset(a, pump) is None  # already here: nothing moved
    assert ct.add_asset(b, pump) == a
    pump.refresh_from_db()
    assert pump.contract == b and a.covered_assets().count() == 0


def test_add_asset_rejects_retired_devices(ctx, pump):
    pump.status = AssetStatus.RETIRED
    pump.save()
    with pytest.raises(ValidationError, match="retired"):
        ct.add_asset(make_contract(), pump)


def test_add_model_skips_retired_and_already_covered(ctx, dept, pump_model, pump):
    c, other = make_contract(reference="A"), make_contract(reference="B")
    covered = Asset.objects.create(tag="CE-2", device_model=pump_model, department=dept, contract=c)
    on_other = Asset.objects.create(tag="CE-3", device_model=pump_model, department=dept, contract=other)
    Asset.objects.create(tag="CE-4", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    assert [(dm.model, dm.n) for dm in ct.model_options(c)] == [("Alaris 8015 PCU", 2)]  # pump (in-house) and CE-3 (on B)
    assert ct.add_model(c, pump_model) == 2
    assert set(c.covered_assets().values_list("tag", flat=True)) == {pump.tag, covered.tag, on_other.tag}
    assert not ct.model_options(c).exists()


def test_remove_asset_only_from_its_own_contract(ctx, vent):
    a, b = make_contract(reference="A"), make_contract(reference="B")
    ct.add_asset(a, vent)
    with pytest.raises(ValidationError, match="not on B"):
        ct.remove_asset(b, vent)
    ct.remove_asset(a, vent)
    vent.refresh_from_db()
    assert vent.contract is None


def test_contract_status_for_each_state(ctx):
    expired = make_contract(reference="E", start=TODAY - timedelta(days=400), end=TODAY - timedelta(days=22))
    ending = make_contract(reference="S", end=TODAY + timedelta(days=45))
    future = make_contract(reference="F", start=TODAY + timedelta(days=10), end=TODAY + timedelta(days=375))
    active = make_contract(reference="A", end=TODAY + timedelta(days=200))
    assert ct.contract_status(expired) == {"key": "expired", "label": f"Expired {ct.fmt_short(expired.end_on)}", "css": "crit"}
    assert ct.contract_status(ending) == {"key": "ending", "label": "Ends in 45 d", "css": "warn"}
    assert ct.contract_status(future) == {"key": "active", "label": f"Starts {ct.fmt_short(future.start_on)}", "css": "info"}
    assert ct.contract_status(active) == {"key": "active", "label": "Active", "css": "ok"}
    assert ct.contract_status(active, today=TODAY + timedelta(days=150))["key"] == "ending"


def test_filter_contracts_by_status_type_and_text(ctx, vent, pump):
    make_contract(reference="OLD-1", vendor="Steris", start=TODAY - timedelta(days=400), end=TODAY - timedelta(days=1), type=ContractType.THIRD_PARTY)
    ending = make_contract(reference="SOON-1", vendor="BD", end=TODAY + timedelta(days=90), coverage=Coverage.PARTS)
    active = make_contract(reference="SC-1", vendor="Hamilton Medical", end=TODAY + timedelta(days=91))
    ct.add_asset(active, vent)
    ct.add_asset(ending, pump)

    def refs(**kw):
        return [c.reference for c in ct.filter_contracts(ct.ContractFilters(**kw))]

    assert refs() == ["OLD-1", "SOON-1", "SC-1"]  # by end date
    assert refs(status="expired") == ["OLD-1"] and refs(status="ending") == ["SOON-1"] and refs(status="active") == ["SC-1"]
    assert refs(type="third_party") == ["OLD-1"]
    assert refs(q="steris") == ["OLD-1"] and refs(q="parts only") == ["SOON-1"] and refs(q="G5") == ["SC-1"] and refs(q="alaris") == ["SOON-1"]
    assert refs(q="nothing") == []
    assert [c.devices for c in ct.filter_contracts(ct.ContractFilters())] == [0, 1, 1]


def test_contracts_summary_numbers(ctx, dept, pump_model, vent, pump):
    expired = make_contract(reference="OLD", start=TODAY - timedelta(days=400), end=TODAY - timedelta(days=1), annual_cost=999)
    ending = make_contract(reference="SOON", end=TODAY + timedelta(days=30), annual_cost=1000)
    make_contract(reference="SC", end=TODAY + timedelta(days=300), annual_cost=3120)
    ct.add_asset(expired, pump)
    ct.add_asset(ending, vent)
    Asset.objects.create(tag="R-1", device_model=pump_model, department=dept, status=AssetStatus.RETIRED, acquisition_cost=9999, contract=ending)
    s = ct.contracts_summary()
    assert (s["contracts"], s["active_fleet"], s["covered"], s["expired"], s["ending"], s["renewals"], s["uncovered"]) == (3, 2, 1, 1, 1, 2, 1)
    assert s["annual"] == 4120 and round(s["annual_pct"], 1) == 10.0 and s["covered_pct"] == 50.0  # fleet value 41,200; retired excluded


def test_summary_with_an_empty_fleet_has_no_division_error(ctx):
    s = ct.contracts_summary()
    assert s["annual_pct"] == 0.0 and s["covered_pct"] == 0.0


def test_covered_models_and_device_list(ctx, dept, vent_model, pump_model, vent, pump):
    c = make_contract()
    ct.add_model(c, pump_model)
    ct.add_asset(c, vent)
    for i in range(45):
        Asset.objects.create(tag=f"P-{i:03}", device_model=pump_model, department=dept, contract=c)
    assert ct.covered_models(c) == [("BD Alaris 8015 PCU", 46), ("Hamilton Medical Hamilton-G5", 1)]
    lst = ct.covered_devices(c)
    assert (lst["total"], lst["matched"], len(lst["devices"])) == (47, 47, 40)
    assert lst["devices"][0].device_model == pump_model  # by model, then tag
    filtered = ct.covered_devices(c, "g5")
    assert (filtered["matched"], [a.tag for a in filtered["devices"]]) == (1, [vent.tag])
    assert ct.covered_devices(c, "ICU")["matched"] == 47


def test_pick_devices_excludes_retired_and_already_covered(ctx, dept, pump_model, vent, pump):
    c = make_contract()
    ct.add_asset(c, pump)
    Asset.objects.create(tag="CE-R", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    assert [a.tag for a in ct.pick_devices(c, "CE-")] == [vent.tag]
    assert not ct.pick_devices(c, "C").exists()


def test_create_rejects_bad_choices_missing_dates_and_unknown_fields(ctx):
    with pytest.raises(ValidationError) as e:
        ct.create_contract(reference="X", vendor="V", type="bogus", coverage="bogus", annual_cost=None)
    assert {"type", "coverage"} <= set(e.value.message_dict)
    with pytest.raises((ValidationError, TypeError)):
        ct.create_contract(reference="X", vendor="V", start_on=TODAY, end_on=TODAY, not_a_field=1)
    assert not Contract.objects.filter(reference="X").exists()


def test_delete_reports_covered_devices_but_detaches_retired_ones_too(ctx, dept, pump_model, vent, pump):
    c = make_contract()
    ct.add_asset(c, vent)
    ct.add_asset(c, pump)
    pump.status = AssetStatus.RETIRED
    pump.save()
    assert ct.delete_contract(c) == 1  # the toast counts covered devices, as the confirm text does
    pump.refresh_from_db()
    vent.refresh_from_db()
    assert pump.contract_id is None and vent.contract_id is None
