"""
Slice 31, vendor staff: engineers, their training, the vendors' attestations, and vendor jobs, made through apps.vendors.services and
the work order services as the screens make them. Import from any test file:
`from vendor_fixtures import engineer, training, attestation, vendor_job, VENDOR`.

- engineer(name="Dana Kim", company=VENDOR): a vendor's engineer.
- training(eng, *models, issued_on=<a year ago>, expires_on=None, source=OEM, **coverage): one training on file naming `models` (device
  models), or another coverage when `covers` is given (covers=Covers.LINE, manufacturer=..., category=...).
- attestation(*names, covers=Covers.ALL, valid_from=<a year ago>, valid_through=<a year ahead>, received_on=valid_from, **coverage):
  a vendor's attestation under `names` (default VENDOR).
- vendor_job(asset, vendor_name=VENDOR, engineer=None, engineer_name="", completed_on=None, type_=REPAIR, **completion): a work order on
  vendor service completed through completion.complete_work_order (a repair checked and ready for use unless `completion` says
  otherwise), on `completed_on` (default today). Read again.
"""
from datetime import timedelta

from django.utils import timezone

from apps.vendors import services
from apps.vendors.models import Covers, TrainingSource

VENDOR = "Philips"


def _today():
    return timezone.localdate()


def engineer(name="Dana Kim", company=VENDOR, **kwargs):
    return services.add_engineer(name=name, company=company, **kwargs)


def training(eng, *models, issued_on=None, expires_on=None, source=TrainingSource.OEM, covers=None, today=None, **coverage):
    issued_on = issued_on or _today() - timedelta(days=365)
    if covers is None:
        covers, coverage = Covers.MODELS, {"model_ids": [str(m.pk) for m in models], **coverage}
    return services.add_training([eng], covers=covers, source=source, issued_on=issued_on, expires_on=expires_on, today=today,
                                 **coverage)[0]


def attestation(*names, covers=Covers.ALL, valid_from=None, valid_through=None, received_on=None, reference="LTR-2026-01", today=None,
                **coverage):
    valid_from = valid_from or _today() - timedelta(days=365)
    valid_through = valid_through or _today() + timedelta(days=365)
    received_on = received_on or min(valid_from, _today())
    return services.add_attestation(vendor_names=list(names or [VENDOR]), covers=covers, reference=reference, received_on=received_on,
                                    valid_from=valid_from, valid_through=valid_through, today=today, **coverage)


def vendor_job(asset, vendor_name=VENDOR, *, engineer=None, engineer_name="", completed_on=None, type_=None, **completion):
    from repair_fixtures import CHECKED

    from apps.workorders.completion import complete_work_order
    from apps.workorders.models import Priority, WoStatus, WoType
    from apps.workorders.services import assign, change_status, create_work_order

    type_ = type_ or WoType.REPAIR
    day = completed_on or _today()
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Vendor service visit", opened_on=day - timedelta(days=2))
    assign(wo, vendor_name=vendor_name)
    change_status(wo, WoStatus.IN_PROGRESS, as_of=day)
    extra = dict(CHECKED) if type_ == WoType.REPAIR else {}
    extra.update(completion)
    if engineer is not None:
        extra["vendor_engineer"] = engineer
    if engineer_name:
        extra["engineer_name"] = engineer_name
    complete_work_order(wo, resolution="Serviced by the vendor", today=day, **extra)
    wo.refresh_from_db()
    return wo
