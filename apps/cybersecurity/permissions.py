"""
Access levels for the device security register (slice 37), shared by the web UI and the API so neither is the weaker door. A module of
its own (Module.CYBERSECURITY, "Device security"): IT security staff record facts here without Equipment Edit (which adds, edits, and
retires devices), and every existing custom role reads None for it until a director grants it.

- View sees the register: models' profiles and releases (View alone), and devices' facts (View AND Equipment View: those lists name
  devices by tag and location).
- Edit records a model's profile and releases (and marks one entered in error), and a device's facts (one device, or one model's devices
  at once).
- Slice 38, software updates (apps.cybersecurity.updates): what an update did is recorded (at a completion, by Confirm version, or by
  hand) at Edit and Equipment View (can_edit_devices); opening update work orders also needs Work orders Edit (can_open_updates), and
  assigning them Work orders Approve (workorders.permissions.can_assign: unassigned otherwise); Approve marks a hand-recorded update
  entered in error and clears a work order's recorded outcome (can_correct_updates).
- Slice 39, security decisions (apps.cybersecurity.decisions): reading any decision needs View AND Equipment View (can_view_devices: a
  decision can name a device); recording, superseding, ending, and marking one entered in error need Approve AND Equipment View
  (can_decide).

Scoped users (a vendor's company, a requester's unit) are refused everywhere, whatever their levels: the register is never theirs.
The suggested IT security role (README) is a custom role with Device security Edit and Equipment View; for IT to record its exceptions
as decisions, Device security Approve (with Equipment View it then also marks updates entered in error), and Work orders View to follow
update work orders. Existing custom roles keep their levels.
"""
from apps.accounts.models import Level, Module
from apps.workorders import scoping

MODULE = Module.CYBERSECURITY
VIEW_LEVEL = Level.VIEW
EDIT_LEVEL = Level.EDIT


def can_view(user) -> bool:
    """Models' profiles and releases."""
    return user.has_level(MODULE, VIEW_LEVEL) and not scoping.is_scoped(user)


def can_view_devices(user) -> bool:
    """Devices' facts and every list naming devices: Device security View and Equipment View."""
    return can_view(user) and user.has_level(Module.EQUIPMENT, Level.VIEW)


def can_edit(user) -> bool:
    """A model's profile and releases."""
    return user.has_level(MODULE, EDIT_LEVEL) and not scoping.is_scoped(user)


def can_edit_devices(user) -> bool:
    """A device's facts (one, or one model's devices at once)."""
    return can_edit(user) and user.has_level(Module.EQUIPMENT, Level.VIEW)


APPROVE_LEVEL = Level.APPROVE


def can_open_updates(user) -> bool:
    """Open software update work orders (slice 38: one device's, or a release's devices'): Device security Edit, Equipment View, and
    Work orders Edit (the level that opens work orders). Assigning them is Work orders Approve (workorders.permissions.can_assign)."""
    from apps.workorders import permissions as wo_perms

    return can_edit_devices(user) and user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL)


def can_correct_updates(user) -> bool:
    """Mark a hand-recorded software update entered in error, or clear a work order's recorded outcome (slice 38)."""
    return user.has_level(MODULE, APPROVE_LEVEL) and not scoping.is_scoped(user) and user.has_level(Module.EQUIPMENT, Level.VIEW)


def can_decide(user) -> bool:
    """Record, supersede, or end a security decision, or mark one entered in error (slice 39): Approve and Equipment View."""
    return user.has_level(MODULE, APPROVE_LEVEL) and not scoping.is_scoped(user) and user.has_level(Module.EQUIPMENT, Level.VIEW)
