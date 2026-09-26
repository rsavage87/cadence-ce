from rest_framework.permissions import BasePermission

from apps.accounts.models import Level
from apps.accounts.permissions import level_required_for_method


class ModulePermission(BasePermission):
    """Checks the viewset's `module` against the user's role. Reads need View, writes need `write_level` (Edit), deletes `delete_level` (Full)."""

    def has_permission(self, request, view):
        module = getattr(view, "module", None)
        if module is None or not request.user or not request.user.is_authenticated:
            return False
        write_level = getattr(view, "write_level", Level.EDIT)
        delete_level = getattr(view, "delete_level", Level.FULL)
        return request.user.has_level(module, level_required_for_method(request.method, write_level, delete_level))
