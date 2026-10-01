"""
Auto-assign week on the PM schedule (slice 14): the mock's button. GET shows what it would do (a modal); POST does it through
apps.pm.services and answers like "Create N PM work orders" (toast, `pm-changed`).

TODO(slice 14, part C).
"""
from apps.pm import permissions as pm_perms

from .decorators import web_view


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def week_assign(request):
    raise NotImplementedError  # TODO(part C)
