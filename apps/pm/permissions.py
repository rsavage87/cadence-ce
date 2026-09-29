"""Access levels for the PM schedule, shared by the web UI and the API.

Anyone with PM View sees the schedule. Creating a day's PM work orders needs PM Approve, the same level the API's
nightly `generate` action has always needed: it plans work for the whole shop. The batch assigns each work order only
when the user may also assign work orders (workorders Approve); otherwise the work orders are created unassigned."""
from apps.accounts.models import Level, Module

MODULE = Module.PM
VIEW_LEVEL = Level.VIEW
CREATE_LEVEL = Level.APPROVE


def can_create(user) -> bool:
    return user.has_level(MODULE, CREATE_LEVEL)
