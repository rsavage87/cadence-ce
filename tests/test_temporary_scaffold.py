"""
Slice 29 scaffold: a temporary device (a rental, vendor loaner, or demo unit) added through create_asset costs nothing, has no PM of
ours, and its owner maintains it; a returned one reads "Returned to owner", never "Retired"; the fleet buckets, the counting rule's Q,
and the vendor a work order names.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.contracts.models import Contract
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Ownership, SupportType


def _rental(dept, model, tag="T-0001", **kw):
    today = timezone.localdate()
    return eq.create_asset(tag=tag, device_model=model, department=dept, serial="SN-1", ownership=Ownership.RENTAL, owner="Acme Rentals",
                           owner_reference="RA-2026-0042", arrived_on=today - timedelta(days=3), due_back_on=today + timedelta(days=11),
                           owner_pm_due_on=today + timedelta(days=60), next_pm_on=today + timedelta(days=5), last_pm_on=today, **kw)


def test_a_temporary_device_costs_nothing_and_has_no_pm_of_ours(ctx, dept, pump_model):
    rental = _rental(dept, pump_model)
    assert rental.acquisition_cost == Decimal("0") and rental.next_pm_on is None and rental.last_pm_on is None
    assert rental.installed_on == rental.arrived_on and rental.support_type == SupportType.OWNER
    assert rental.temporary and eq.owner_maintains(rental) and rental.owner == "Acme Rentals"
    ours = eq.create_asset(tag="CE-1", device_model=pump_model, department=dept)
    assert not ours.temporary and ours.support_type == SupportType.IN_HOUSE and ours.next_pm_on is not None
    assert ours.owner == "" and ours.arrived_on is None


def test_a_returned_device_reads_returned_to_owner(ctx, dept, pump_model, vent):
    rental = _rental(dept, pump_model)
    Asset.objects.filter(pk=rental.pk).update(status=AssetStatus.RETIRED)
    rental.refresh_from_db()
    assert eq.status_label(rental) == eq.RETURNED_LABEL
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.RETIRED)
    vent.refresh_from_db()
    assert eq.status_label(vent) == "Retired"


def test_the_buckets_and_the_counting_rule(ctx, dept, pump_model, vent):
    rental = _rental(dept, pump_model)
    returned = _rental(dept, pump_model, tag="T-0002")
    Asset.objects.filter(pk=returned.pk).update(status=AssetStatus.RETIRED)
    buckets = dict(eq.with_bucket(Asset.objects.all(), timezone.localdate()).values_list("tag", "bucket"))
    assert buckets[rental.tag] == eq.FleetBucket.TEMPORARY and buckets[returned.tag] == eq.FleetBucket.RETURNED
    assert buckets[vent.tag] != eq.FleetBucket.TEMPORARY
    assert set(Asset.objects.filter(eq.OWNED).values_list("tag", flat=True)) == {vent.tag}


def test_the_vendor_a_work_order_names(ctx, dept, pump_model, vent):
    assert eq.service_vendor(_rental(dept, pump_model)) == "Acme Rentals"
    assert eq.service_vendor(vent) == "Hamilton Medical field service"
    contract = Contract.objects.create(reference="SC-1", vendor="BioServ", start_on=date(2026, 1, 1),
                                       end_on=date(2027, 1, 1))
    vent.contract = contract
    vent.save()
    assert eq.service_vendor(vent) == "BioServ"


def test_a_unit_here_before_is_found_by_model_and_serial(ctx, dept, pump_model):
    first = _rental(dept, pump_model)
    Asset.objects.filter(pk=first.pk).update(status=AssetStatus.RETIRED, returned_on=timezone.localdate())
    assert list(eq.matching_returned(pump_model, "sn-1")) == [first]
    assert not eq.matching_returned(pump_model, "SN-2").exists()
