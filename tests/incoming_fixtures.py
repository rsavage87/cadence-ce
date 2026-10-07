"""
Shared helpers for slice 26's incoming inspection tests. Import what you need: `from incoming_fixtures import waiting_device`.
"""
from datetime import date

from apps.equipment import services as eq
from apps.equipment.models import Department


def waiting_device(device_model, tag: str = "NEW-1", *, department=None, by=None, today: date | None = None, added_on: date | None = None,
                   inspection_due: date | None = None):
    """A device added new and waiting for its incoming inspection (create_asset's "waiting" path): out of service, no next PM, one
    Incoming inspection work order open and unassigned."""
    department = department or Department.objects.get_or_create(name="Biomed receiving")[0]
    return eq.create_asset(tag=tag, device_model=device_model, department=department, incoming_inspection=eq.INCOMING_WAITING, by=by,
                           today=today, added_on=added_on, inspection_due=inspection_due)
