"""
What every API module shares (slice 19 split apps/api/views.py by area: views.py devices, models, departments, work orders, and
settings; views_work.py a work order's labor, parts, and notes; views_contracts.py; views_pm.py the PM schedule and program;
views_reports.py the Overview, the reports, custom reports, and report emails; views_recalls.py; views_scan.py; views_users.py).

Every view inherits TenantAPIMixin (apps/api/tenancy.py) and checks ModulePermission: ApiViewSet for plain viewsets, TenantViewSet
for model viewsets. Querysets resolve per request through the tenant-scoped manager; never put `queryset = Model.objects.all()` on a
class. State changes go through the services (CLAUDE.md), never a serializer's save on a business row.
"""
import re
from datetime import date

from django.core.exceptions import ValidationError
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated

from apps.tenants.context import get_current_tenant

from .permissions import ModulePermission
from .tenancy import TenantAPIMixin


class TenantViewSet(TenantAPIMixin, viewsets.ModelViewSet):
    model = None
    module = None
    permission_classes = [IsAuthenticated, ModulePermission]
    # Slice 16: the actions that narrow every row they read or write to a scoped user's share. None by default: every other
    # action refuses a scoped user whatever their levels (ModulePermission), so a viewset opts in, never out.
    scoped_actions = frozenset()

    def get_queryset(self):
        return self.model.objects.all()

    def perform_create(self, serializer):
        serializer.save(tenant=get_current_tenant())


class ApiViewSet(TenantAPIMixin, viewsets.ViewSet):
    """A viewset over services rather than one model: set `module` (and `write_level` when writes need more than Edit)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = None
    scoped_actions = frozenset()


def _via_service(fn, *args, **kwargs):
    """Call a service; its ValidationError becomes a 400, field errors keyed by field (as a form shows them), anything else as detail."""
    try:
        return fn(*args, **kwargs)
    except ValidationError as e:
        raise DRFValidationError(e.message_dict if hasattr(e, "error_dict") else {"detail": " ".join(e.messages)}) from e


def _refuse_on_create(data, fields, what):
    """Fields the create service does not take: refused when given rather than dropped, so a client never thinks they were saved."""
    errors = {f: [f"Set this with PATCH once the {what} is added."] for f in fields if data.get(f) not in (None, "")}
    if errors:
        raise DRFValidationError(errors)


ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_day(value):
    """A strict YYYY-MM-DD date, or None (missing, malformed, or not a real day)."""
    if not isinstance(value, str) or not ISO_DAY.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _hours(h) -> str:
    return f"{h:.2f}"
