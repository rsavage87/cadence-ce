"""Who may see and change facility settings. The views and the API check these server-side."""
from apps.accounts.models import Level, Module

MODULE = Module.SETTINGS
VIEW_LEVEL = Level.VIEW  # the manager role sees settings
EDIT_LEVEL = Level.EDIT  # changing the portal, policy, or targets (the director by default)


def can_edit(user) -> bool:
    return user.has_level(MODULE, EDIT_LEVEL)
