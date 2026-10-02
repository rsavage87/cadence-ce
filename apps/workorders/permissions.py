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


def transition_level(from_status: str, to_status: str) -> int:
    if WoStatus.CLOSED in (from_status, to_status):
        return Level.APPROVE
    return Level.EDIT


def can_transition(user, from_status: str, to_status: str) -> bool:
    return user.has_level(MODULE, transition_level(from_status, to_status))


def can_assign(user) -> bool:
    return user.has_level(MODULE, ASSIGN_LEVEL)


def can_record_work(user) -> bool:
    """Add or remove labor and parts lines on a work order that is not closed."""
    return user.has_level(MODULE, RECORD_LEVEL)
