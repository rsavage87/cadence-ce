from datetime import date, timedelta

from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, SupportType


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
