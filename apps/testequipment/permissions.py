"""
Access levels for test equipment (slice 35), shared by the web UI and the API so neither is the weaker door. Test equipment is part of
the Equipment module (no module of its own: a new module would give every custom role None, and change who may refresh a program
evaluation, whose figures already need Equipment and Work orders View).

- View (Equipment View) sees the list, an item, its calibrations, its reviews, and the work done with it.
- Edit (Equipment Edit, technicians and CE managers by default) adds an item (with its latest certificate), changes its details (not its
  interval once a certificate is on file: the interval caps a certificate's due day), moves its everyday status (out for calibration,
  back from calibration, out of service when damaged or after a failed check, missing, found, returned to its owner), and records a
  calibration.
- Decide (Work orders Approve with Equipment View: the CE manager by default, the level that already decides about completed work)
  records the tolerance review, marks a calibration entered in error, and changes the interval once a certificate is on file.
- Equipment Approve (the director by default, as for devices) retires an item worn out or lost, and reinstates a retired one.

Scoped users (a vendor's company, a requester's unit) are refused everywhere, even with Equipment View on a custom role: the facility's
test equipment and its records are never theirs, and they never record test equipment on a work order. Recording which test equipment a
work order used is the work order's (completing it: Work orders Edit; afterwards workorders.permissions.can_set_test_equipment).
"""
from apps.accounts.models import Level, Module
from apps.workorders import scoping

MODULE = Module.EQUIPMENT
VIEW_LEVEL = Level.VIEW
EDIT_LEVEL = Level.EDIT
RETIRE_LEVEL = Level.APPROVE
DECIDE_MODULE = Module.WORKORDERS
DECIDE_LEVEL = Level.APPROVE


def can_view(user) -> bool:
    return user.has_level(MODULE, VIEW_LEVEL) and not scoping.is_scoped(user)


def can_edit(user) -> bool:
    return user.has_level(MODULE, EDIT_LEVEL) and not scoping.is_scoped(user)


def can_decide(user) -> bool:
    """The tolerance review, entered in error, the interval once a certificate is on file."""
    return user.has_level(DECIDE_MODULE, DECIDE_LEVEL) and can_view(user)


def can_retire(user) -> bool:
    """Retire (worn out, lost) and reinstate."""
    return user.has_level(MODULE, RETIRE_LEVEL) and not scoping.is_scoped(user)
