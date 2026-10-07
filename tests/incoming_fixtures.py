"""
Shared helpers for slice 26's incoming inspection tests. Import what you need: `from incoming_fixtures import waiting_device`.

- waiting_device: a device added new and waiting for its incoming inspection (create_asset's "waiting" path).
- INCOMING_ALL_PASS / incoming_results: results for the incoming checklist (inspections.INCOMING_CHECKLIST), with a leakage reading.
- passed_device: a waiting device whose inspection a technician passed (in service, PM clock started).
- failed_device: a waiting device whose inspection failed, with its re-inspection open.
- in_use_device: a waiting device put in use before its inspection (use_before_inspection), still waiting.
Each takes `today` (the day the device was added and its inspection done) and returns what the test needs, read again.
"""
from datetime import date

from django.utils import timezone

from apps.equipment import services as eq
from apps.equipment.models import Department, UseBeforeInspection
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult
from apps.workorders.services import assign

LEAKAGE = "42"


def waiting_device(device_model, tag: str = "NEW-1", *, department=None, by=None, today: date | None = None, added_on: date | None = None,
                   inspection_due: date | None = None, installed_on: date | None = None):
    """A device added new and waiting for its incoming inspection (create_asset's "waiting" path): out of service, no next PM, one
    Incoming inspection work order open and unassigned."""
    department = department or Department.objects.get_or_create(name="Biomed receiving")[0]
    return eq.create_asset(tag=tag, device_model=device_model, department=department, incoming_inspection=eq.INCOMING_WAITING, by=by,
                           today=today, added_on=added_on, inspection_due=inspection_due, installed_on=installed_on)


def incoming_results(*values, reading: str = LEAKAGE) -> list[dict]:
    """Results for the incoming checklist, "pass" / "fail" / "na" per step (all pass when none given), the leakage reading on step 2."""
    values = values or ("pass",) * len(inspections.INCOMING_CHECKLIST)
    return [{"result": v, "reading": reading if measure and v != "na" else ""}
            for v, (_text, measure) in zip(values, inspections.INCOMING_CHECKLIST, strict=True)]


INCOMING_ALL_PASS = incoming_results()


def _inspect(asset, technician=None, vendor: str = "", **kwargs):
    wo = inspections.open_inspection(asset)
    if technician is not None or vendor:
        assign(wo, technician=technician, vendor_name=vendor)
    return wo, complete_work_order(wo, **kwargs)


def passed_device(device_model, tag: str = "NEW-2", *, technician=None, vendor: str = "", today: date | None = None, by=None):
    """(device, its passed inspection): a waiting device whose inspection was completed Passed on `today` with the incoming checklist
    all passed, by `technician` (or `vendor`)."""
    today = today or timezone.localdate()
    asset = waiting_device(device_model, tag, today=today)
    wo, _done = _inspect(asset, technician, vendor, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS, by=by, today=today)
    asset.refresh_from_db()
    wo.refresh_from_db()
    return asset, wo


def failed_device(device_model, tag: str = "NEW-3", *, technician=None, vendor: str = "", today: date | None = None, by=None):
    """(device, its failed inspection, the re-inspection it opened): failed on `today`, step 2's leakage too high."""
    today = today or timezone.localdate()
    asset = waiting_device(device_model, tag, today=today)
    wo, done = _inspect(asset, technician, vendor, inspection_result=InspectionResult.FAILED,
                        results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"),
                        resolution="Leakage 640 µA, over the limit; vendor to swap the unit", by=by, today=today)
    asset.refresh_from_db()
    wo.refresh_from_db()
    return asset, wo, done.reinspection


def in_use_device(device_model, tag: str = "NEW-4", *, reason: str = UseBeforeInspection.EMERGENCY, by=None, today: date | None = None):
    """A waiting device put in use before its inspection (in service, still waiting, no next PM), its inspection high priority."""
    today = today or timezone.localdate()
    asset = waiting_device(device_model, tag, today=today)
    return eq.use_before_inspection(asset, reason, by=by, today=today)
