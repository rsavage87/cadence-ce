"""
Access levels for work order actions, shared by the web UI and the API so neither is the weaker door.

Per the default role descriptions, CE managers "assign and close work": assigning and closing
(or reopening a closed work order) need Approve. Other status changes need Edit.
"""
from apps.accounts.models import Level, Module

from .models import WoStatus

MODULE = Module.WORKORDERS
CREATE_LEVEL = Level.EDIT
NOTE_LEVEL = Level.EDIT
ASSIGN_LEVEL = Level.APPROVE
# Slice 15: recording the work. Labor and parts on a work order need Edit (technicians log their own time), as does completing it
# (transition_level). A closed work order is the record: its lines are fixed until it is reopened (Approve).
RECORD_LEVEL = Level.EDIT
# A labor line charged at a rate other than Settings' (in-house or vendor) needs Approve: the Reports' spend is built on the rate.
RATE_LEVEL = Level.APPROVE
# Slice 24: a technician taking open work nobody has, on a device they are credentialed for, needs Edit (the level they work it at);
# assigning anyone else stays Approve. apps.workorders.services.take has the rest of the rule (the facility's setting among it).
TAKE_LEVEL = Level.EDIT


def transition_level(from_status: str, to_status: str) -> int:
    if WoStatus.CLOSED in (from_status, to_status):
        return Level.APPROVE
    return Level.EDIT


def can_transition(user, from_status: str, to_status: str) -> bool:
    return user.has_level(MODULE, transition_level(from_status, to_status))


def can_assign(user) -> bool:
    return user.has_level(MODULE, ASSIGN_LEVEL)


def can_take(user) -> bool:
    """Take an unassigned work order for oneself (apps.workorders.services.take, which checks the rest)."""
    return user.has_level(MODULE, TAKE_LEVEL)


def can_record_work(user) -> bool:
    """Add or remove labor and parts lines on a work order that is not closed."""
    return user.has_level(MODULE, RECORD_LEVEL)


def can_set_rate(user) -> bool:
    """Log time at a rate other than the Settings rate for the work order (apps.workorders.costs.default_rate)."""
    return user.has_level(MODULE, RATE_LEVEL)


# Slice 25: why a PM missed its due date (WorkOrder.late_reason, for the survey binder). Recording it is documenting the work, so Edit,
# as recording time is, while the work order is open, completed, or cancelled; a closed work order is the record, so Approve then.
LATE_REASON_LEVEL = Level.EDIT


def late_reason_level(status: str) -> int:
    return Level.APPROVE if status == WoStatus.CLOSED else LATE_REASON_LEVEL


def can_set_late_reason(user, wo) -> bool:
    """Record or change why `wo` (a PM that missed its due date: apps.pm.services.missed_pms) was late. Scoped users only on what they
    can see (the caller narrows through apps.workorders.scoping, as for every move)."""
    return user.has_level(MODULE, late_reason_level(wo.status))
