"""
Access levels for device incidents (slice 28), shared by the web UI and the API so neither is the weaker door.

The on-call technician records an incident and holds the device at 2 a.m. (Edit); the people who decide whether it was reportable and
send the reports (risk management with the CE manager: Approve) decide, record the reports, release the device, and close it.
Holding a device also needs Equipment Edit (tagging it out), opening a work order Work orders Edit, and returning a device to use
Equipment Edit: an incident is never a side door around those screens. Scoped users (a vendor's company, a requester's unit) are
refused everywhere: incidents are never theirs (the device and its work order show only "Held by Clinical Engineering").
"""
from apps.accounts.models import Level, Module
from apps.equipment import permissions as eq_perms
from apps.workorders import permissions as wo_perms
from apps.workorders import scoping

MODULE = Module.INCIDENTS
VIEW_LEVEL = Level.VIEW
# Record an incident, hold another device, record the finding, the accessories and the event log, the event report number, raise
# the outcome, move "clinical staff first knew on" earlier, open an investigation work order.
RECORD_LEVEL = Level.EDIT
# The reportability decision, the reports sent, lowering the outcome, moving "first knew on" later, sending a held device to the
# manufacturer and taking it back, releasing a hold, closing and reopening, and "recorded in error".
DECIDE_LEVEL = Level.APPROVE

HOLD_EQUIPMENT_LEVEL = Level.EDIT  # tagging a device out (equipment.permissions: Edit to tag out)
RETURN_EQUIPMENT_LEVEL = Level.EDIT  # returning it to service


def can_view(user) -> bool:
    return user.has_level(MODULE, VIEW_LEVEL) and not scoping.is_scoped(user)


def can_record(user) -> bool:
    return user.has_level(MODULE, RECORD_LEVEL) and not scoping.is_scoped(user)


def can_decide(user) -> bool:
    return user.has_level(MODULE, DECIDE_LEVEL) and not scoping.is_scoped(user)


def can_hold(user) -> bool:
    """Record with a hold, or hold another device: Incidents Edit and Equipment Edit."""
    return can_record(user) and user.has_level(eq_perms.MODULE, HOLD_EQUIPMENT_LEVEL)


def can_open_work_order(user) -> bool:
    return can_record(user) and user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL)


def can_return_to_use(user) -> bool:
    return can_decide(user) and user.has_level(eq_perms.MODULE, RETURN_EQUIPMENT_LEVEL)
