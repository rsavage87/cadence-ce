"""
Access levels for recall actions, shared by the web UI and the API so neither is the weaker door.

Per the default role descriptions, CE managers "approve recall closures": closing an alert (closed or
reviewed-not-affected) and reopening one need Approve. Marking under review, creating the recall work
orders, and re-running the inventory match need Edit.
"""
from apps.accounts.models import Level, Module

from .models import AlertMatch

MODULE = Module.RECALLS
REVIEW_LEVEL = Level.EDIT
CLOSE_LEVEL = Level.APPROVE
WORK_ORDERS_LEVEL = Level.EDIT
MATCH_LEVEL = Level.EDIT

DONE_STATUSES = (AlertMatch.Status.CLOSED, AlertMatch.Status.NOT_AFFECTED)


def transition_level(from_status: str, to_status: str) -> int:
    if from_status in DONE_STATUSES or to_status in DONE_STATUSES:
        return CLOSE_LEVEL
    return REVIEW_LEVEL


def can_transition(user, from_status: str, to_status: str) -> bool:
    return user.has_level(MODULE, transition_level(from_status, to_status))
