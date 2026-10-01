"""Access levels for the PM schedule and the PM program, shared by the web UI and the API.

Anyone with PM View sees the schedule, the PM library, and each model's PM program. Creating a day's PM work orders needs PM
Approve, the same level the API's nightly `generate` action has always needed: it plans work for the whole shop. The batch
assigns each work order only when the user may also assign work orders (workorders Approve); otherwise the work orders are
created unassigned. Auto-assign week (slice 14) assigns, so it needs both.

The PM program (slice 14): writing PM procedures, choosing a model's procedure, and proposing an AEM interval need PM Edit;
approving, rejecting, or ending an AEM interval needs PM Approve (the roles screen: "Approve adds sign-off actions ... approving
AEM changes"; the CE manager by default). A model's details and risk score are equipment data: apps.equipment.permissions."""
from apps.accounts.models import Level, Module

MODULE = Module.PM
VIEW_LEVEL = Level.VIEW
CREATE_LEVEL = Level.APPROVE
PROGRAM_EDIT_LEVEL = Level.EDIT
AEM_DECIDE_LEVEL = Level.APPROVE


def can_create(user) -> bool:
    return user.has_level(MODULE, CREATE_LEVEL)


def can_assign_week(user) -> bool:
    """Auto-assign week creates and assigns: PM Approve and the work-order assign level."""
    from apps.workorders import permissions as wo_perms

    return can_create(user) and wo_perms.can_assign(user)


def can_edit_program(user) -> bool:
    """Write PM procedures, choose a model's procedure, propose an AEM interval."""
    return user.has_level(MODULE, PROGRAM_EDIT_LEVEL)


def can_decide_aem(user) -> bool:
    """Approve or reject an AEM proposal, or end an AEM interval in force."""
    return user.has_level(MODULE, AEM_DECIDE_LEVEL)
