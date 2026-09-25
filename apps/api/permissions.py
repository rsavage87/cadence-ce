from rest_framework.permissions import BasePermission

from apps.accounts.models import Level
from apps.accounts.permissions import level_required_for_method


class ModulePermission(BasePermission):
    """Checks the viewset's `module` against the user's role. Reads need View, writes need Edit, deletes need Full."""

    def has_permission(self, request, view):
        module = getattr(view, "module", None)
        if module is None or not request.user or not request.user.is_authenticated:
            return False
        write_level = getattr(view, "write_level", Level.EDIT)
        return request.user.has_level(module, level_required_for_method(request.method, write_level))
