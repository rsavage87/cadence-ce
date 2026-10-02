"""
Access levels for changing devices (slice 12), shared by the web UI and the API so neither is the weaker door.

Adding, editing, and the everyday status changes (tag out, return to service, lend, missing, found) need Equipment Edit, which
technicians and CE managers hold by default. Retiring a device (it cancels its open PM work orders and leaves the schedule) and
reinstating a retired one need Approve: the director by default; a facility can give its CE manager Approve in the Roles matrix.

Device models (slice 14): adding one or editing its details needs Edit, like a device. Its risk class decides how it is maintained
(life support never goes on AEM, PM priority, the compliance targets), so scoring a model or changing its risk class needs Approve,
on the screen and in the API.

Marking a model as equipment CMS keeps on the manufacturer's schedule (imaging, radiologic, medical laser; slice 18), or clearing
the mark, decides compliance the same way (a marked model never goes on AEM, and marking it ends an AEM in force), so it needs
Approve too: on Add model and Edit details, and in the API. apps.equipment.services checks it again for any change made by a user.
"""
from apps.accounts.models import Level, Module

from .models import AssetStatus

MODULE = Module.EQUIPMENT
ADD_LEVEL = Level.EDIT
EDIT_LEVEL = Level.EDIT
STATUS_LEVEL = Level.EDIT
RETIRE_LEVEL = Level.APPROVE
MODEL_EDIT_LEVEL = Level.EDIT
RISK_LEVEL = Level.APPROVE
OEM_SCHEDULE_LEVEL = Level.APPROVE


def can_add(user) -> bool:
    return user.has_level(MODULE, ADD_LEVEL)


def can_edit(user) -> bool:
    return user.has_level(MODULE, EDIT_LEVEL)


def status_level(from_status: str, to_status: str) -> int:
    return RETIRE_LEVEL if AssetStatus.RETIRED in (from_status, to_status) else STATUS_LEVEL


def can_set_status(user, from_status: str, to_status: str) -> bool:
    return user.has_level(MODULE, status_level(from_status, to_status))


def can_edit_model(user) -> bool:
    """Add a device model or edit its details (not its risk class)."""
    return user.has_level(MODULE, MODEL_EDIT_LEVEL)


def can_set_risk(user) -> bool:
    """Score a model's risk or change its risk class."""
    return user.has_level(MODULE, RISK_LEVEL)


def can_set_oem_schedule(user) -> bool:
    """Mark a model as keeping the manufacturer's schedule (CMS), or clear the mark."""
    return user.has_level(MODULE, OEM_SCHEDULE_LEVEL)
