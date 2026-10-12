"""
Access levels for vendor staff records (slice 31), shared by the web UI and the API so neither is the weaker door. They are part of the
Contracts module (the vendor coordinator's screen; Users and access is the facility's own accounts and staff): View sees the engineers,
their training, and the attestations; Edit adds and changes them. Scoped users (a vendor's company, a requester's unit) are refused
everywhere, even with Contracts View on a custom role: other vendors' engineers and the facility's evidence are never theirs. Naming the
engineer on a work order is the work order's (completing it: Work orders Edit; afterwards workorders.permissions.can_set_vendor_engineer).
"""
from apps.accounts.models import Level, Module
from apps.workorders import scoping

MODULE = Module.CONTRACTS
VIEW_LEVEL = Level.VIEW
EDIT_LEVEL = Level.EDIT


def can_view(user) -> bool:
    return user.has_level(MODULE, VIEW_LEVEL) and not scoping.is_scoped(user)


def can_edit(user) -> bool:
    return user.has_level(MODULE, EDIT_LEVEL) and not scoping.is_scoped(user)
