from rest_framework.permissions import BasePermission

from apps.accounts.models import Level
from apps.accounts.permissions import level_required_for_method
from apps.workorders import scoping


class ModulePermission(BasePermission):
    """Checks the viewset's `module` against the user's role. Reads need View, writes need `write_level` (Edit), deletes `delete_level` (Full).

    Slice 16, closed by default: a user whose role sees only part of the facility (apps.workorders.scoping: the vendor technician's
    company, the clinical requester's unit) is refused (403) by every endpoint, whatever their levels, except the actions a view
    names in `scoped_actions`. A view names an action only when every row it reads or writes is narrowed to the user's share, so
    a new endpoint, or a new action on a scoped viewset, stays closed to them until it does."""

    SCOPED_REFUSAL = ("Your role sees only part of this facility (its company's or its unit's work orders and devices); "
                      "this endpoint is not limited to that part.")

    def has_permission(self, request, view):
        module = getattr(view, "module", None)
        if module is None or not request.user or not request.user.is_authenticated:
            return False
        write_level = getattr(view, "write_level", Level.EDIT)
        delete_level = getattr(view, "delete_level", Level.FULL)
        if not request.user.has_level(module, level_required_for_method(request.method, write_level, delete_level)):
            return False
        if scoping.is_scoped(request.user) and getattr(view, "action", None) not in getattr(view, "scoped_actions", ()):
            self.message = self.SCOPED_REFUSAL  # DRF builds the permission per request, so the message is this request's
            return False
        return True
