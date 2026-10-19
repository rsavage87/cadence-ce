"""
Slice 35, test equipment made through apps.testequipment.services and the work order's writers, as the screens make them. Import from
any test file: `from testequipment_fixtures import te_world, item, certificate, pm_with, ...` (te_world is a pytest fixture: import it
into the test module; it needs conftest's ctx, pump, and vent).

- item(tag="TE-01", **over): an item added through services.add_test_equipment (in use; no certificate unless `calibration` gives one).
- certificate(item, calibrated_on, **over): a certificate through services.record_calibration (passed, in tolerance, an accreditation
  mark, the due day by default: calibrated_on plus the item's interval).
- in_date(tag), past_due(tag), no_record(tag), failed(tag): one item in each standing judged today (in date: calibrated 100 days ago;
  past due: calibrated 400 days ago, due 35 days ago; no calibration on file; failed 10 days ago, so out of service).
- bench_tech(name="Bench Technician"): a technician without an account (nobody is emailed).
- work_with(asset, type_, *items, completed_on=None, tech=None, **completion): in-house work of `type_` completed on `completed_on`
  (default today) recording `items`, through completion.complete_work_order; pm_with / repair_with (checked and ready for use) /
  safety_with for each type.
- vendor_repair(asset, completed_on=None, **completion): a repair on vendor service completed as checked by the vendor (no test equipment).
- imported_work(asset, completed_on=None, legacy_number="OLD-0001"): a repair imported from another system, completed.
- out_of_tolerance(asset, tag, reviewed=False): an item with a passed certificate 200 days ago, two PMs done with it 100 and 50 days ago,
  and a certificate 10 days ago that found it out of tolerance (adjusted, passed); with `reviewed`, the review decided on it today
  (within margin). A SimpleNamespace: item, before, found, work (the PMs), review.
- te_world (pytest fixture): a SimpleNamespace of all of the above on conftest's pump and vent: in_date, past_due, no_record, failed,
  pm (a completed in-house PM on the pump recording in_date), vendor (a vendor repair on the pump), imported (an imported repair on the
  vent), unreviewed and reviewed (out_of_tolerance on the vent, "TE-OT1" not reviewed and "TE-OT2" reviewed), tech, today.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.testequipment import services
from apps.testequipment.models import AsFound, Basis, Kind, Mark, Program, Result

DAY = timedelta(days=1)


def _today():
    return timezone.localdate()


def item(tag="TE-01", *, kind=Kind.ELECTRICAL_SAFETY, manufacturer="Fluke Biomedical", model="ESA612", serial=None,
         program=Program.CALIBRATION, interval_months=12, held_by=None, calibration=None, by=None, today=None):
    return services.add_test_equipment(tag=tag, kind=kind, manufacturer=manufacturer, model=model, serial=serial or f"SN-{tag}",
                                       program=program, interval_months=interval_months, held_by=held_by, calibration=calibration, by=by,
                                       today=today)


def certificate(te, calibrated_on, *, due_on=None, result=Result.PASSED, as_found=AsFound.IN_TOLERANCE, adjusted=False,
                provider="Fluke Biomedical", number=None, accreditation=Mark.SHOWN, by=None, today=None):
    return services.record_calibration(te, calibrated_on=calibrated_on, due_on=due_on, provider=provider,
                                       certificate=number or f"C-{te.tag}-{calibrated_on:%Y%m%d}", accreditation=accreditation,
                                       as_found=as_found, adjusted=adjusted, result=result, by=by, today=today)


def in_date(tag="TE-IN", **over):
    te = item(tag, **over)
    certificate(te, _today() - 100 * DAY)
    return te


def past_due(tag="TE-PD", **over):
    te = item(tag, **over)
    certificate(te, _today() - 400 * DAY, due_on=_today() - 35 * DAY)
    return te


def no_record(tag="TE-NR", **over):
    return item(tag, **over)


def failed(tag="TE-FL", **over):
    te = item(tag, **over)
    certificate(te, _today() - 10 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    te.refresh_from_db()
    return te


def bench_tech(name="Bench Technician"):
    from apps.credentials.models import Technician

    return Technician.objects.get_or_create(name=name)[0]


def work_with(asset, type_, *items, completed_on=None, tech=None, by=None, **completion):
    """In-house work of `type_` on `asset` completed on `completed_on` (default today) recording `items`. Read again."""
    from repair_fixtures import CHECKED

    from apps.workorders.completion import complete_work_order
    from apps.workorders.models import PmResult, Priority, WoStatus, WoType
    from apps.workorders.services import change_status, create_work_order

    day = completed_on or _today()
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Scheduled work", opened_on=day - 2 * DAY,
                           assigned_to=tech or bench_tech())
    change_status(wo, WoStatus.IN_PROGRESS, as_of=day)
    extra = dict(CHECKED) if type_ == WoType.REPAIR else {"pm_result": PmResult.PASS} if type_ == WoType.PM else {}
    extra.update(completion)
    complete_work_order(wo, resolution="Done per the procedure", test_equipment=[str(i.pk) for i in items], by=by, today=day, **extra)
    wo.refresh_from_db()
    return wo


def pm_with(asset, *items, **kwargs):
    from apps.workorders.models import WoType

    return work_with(asset, WoType.PM, *items, **kwargs)


def repair_with(asset, *items, **kwargs):
    from apps.workorders.models import WoType

    return work_with(asset, WoType.REPAIR, *items, **kwargs)


def safety_with(asset, *items, **kwargs):
    from apps.workorders.models import WoType

    return work_with(asset, WoType.SAFETY, *items, **kwargs)


def vendor_repair(asset, *, completed_on=None, **completion):
    from vendor_fixtures import vendor_job

    from apps.workorders.models import RepairCheck

    return vendor_job(asset, completed_on=completed_on, **{"repair_check": RepairCheck.VENDOR_REPORT, "results": None,
                                                           "checks_reference": "FSO-1001", **completion})


def imported_work(asset, *, completed_on=None, legacy_number="OLD-0001"):
    from apps.workorders import legacy
    from apps.workorders.models import WoStatus, WoType

    day = completed_on or _today() - 30 * DAY
    return legacy.import_work_order(asset=asset, legacy_number=legacy_number, type=WoType.REPAIR, status=WoStatus.COMPLETED,
                                    opened_on=day - 3 * DAY, completed_on=day).work_order


def out_of_tolerance(asset, tag="TE-OT", *, reviewed=False, kind=Kind.INFUSION, model="IDA-5", **over):
    today = _today()
    te = item(tag, kind=kind, model=model, **over)
    before = certificate(te, today - 200 * DAY)
    work = [pm_with(asset, te, completed_on=today - 100 * DAY), pm_with(asset, te, completed_on=today - 50 * DAY)]
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    review = None
    if reviewed:
        review = services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=None, by=None)
    te.refresh_from_db()
    return SimpleNamespace(item=te, before=before, found=found, work=work, review=review)


@pytest.fixture
def te_world(ctx, pump, vent):
    today = _today()
    tech = bench_tech()
    world = SimpleNamespace(today=today, tech=tech, in_date=in_date(held_by=tech), past_due=past_due(), no_record=no_record(),
                            failed=failed())
    world.pm = pm_with(pump, world.in_date, tech=tech)
    world.vendor = vendor_repair(pump, completed_on=today - 5 * DAY)
    world.imported = imported_work(vent)
    world.unreviewed = out_of_tolerance(vent, "TE-OT1")
    world.reviewed = out_of_tolerance(vent, "TE-OT2", reviewed=True, model="IDA-6")
    return world
