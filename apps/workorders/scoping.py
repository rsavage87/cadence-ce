"""
Who sees which devices and work orders inside their facility (slice 16). Tenant isolation keeps facilities apart; this keeps a role
to its own share of one facility, as the default roles describe it:

- The vendor technician (DataScope.COMPANY) sees only work orders assigned to their company: vendor service under the name on the
  user (User.company), as work orders name the vendor (a contract's vendor, or "<manufacturer> field service", web.forms
  .vendor_name_for), in any letter case. And only the devices those work orders are on.
- The clinical requester (DataScope.DEPARTMENT) sees only their own unit: the devices in the Department named like their
  User.department (any letter case), and those devices' work orders.
- Everyone else (DataScope.FACILITY), and superusers, see the whole facility.

A scoped user with no company or no matching department sees nothing (closed, never open). Every list, drawer, search, export,
print, count, and API endpoint that shows devices or work orders filters through work_orders() and assets(); a single record is
checked with can_see_work_order() / can_see_asset(), and one out of scope is a 404 (as another facility's would be).
Call these inside the request's tenant: the role is a tenant-scoped row.
"""
from django.db.models import Exists, OuterRef, Q

from apps.accounts.models import DataScope
from apps.equipment.models import Asset

from .models import WorkOrder

FIELD_SERVICE_SUFFIX = " field service"  # web.forms.vendor_name_for's name for a manufacturer's service without a contract


def scope_of(user) -> str:
    if getattr(user, "is_superuser", False) or not getattr(user, "role_id", None):
        return DataScope.FACILITY  # a user without a role has no level anywhere, so sees nothing anyway
    return user.role.effective_scope


def is_scoped(user) -> bool:
    return scope_of(user) != DataScope.FACILITY


def _nothing() -> Q:
    return Q(pk__in=[])


def company_q(company: str, prefix: str = "") -> Q:
    """Work orders assigned to `company`'s service (prefix "work_orders__" etc. to reach them from another model)."""
    company = (company or "").strip()
    if not company:
        return _nothing()
    return Q(**{f"{prefix}vendor_service": True}) & (Q(**{f"{prefix}vendor_name__iexact": company})
                                                    | Q(**{f"{prefix}vendor_name__iexact": company + FIELD_SERVICE_SUFFIX}))


def _department(user) -> str:
    return (getattr(user, "department", "") or "").strip()


def work_orders(user, qs=None):
    """The work orders `user` may see: `qs` (default every work order in the tenant) narrowed to their scope."""
    qs = WorkOrder.objects.all() if qs is None else qs
    scope = scope_of(user)
    if scope == DataScope.COMPANY:
        return qs.filter(company_q(user.company))
    if scope == DataScope.DEPARTMENT:
        dept = _department(user)
        return qs.filter(asset__department__name__iexact=dept) if dept else qs.none()
    return qs


def assets(user, qs=None):
    """The devices `user` may see: `qs` (default every device in the tenant) narrowed to their scope."""
    qs = Asset.objects.all() if qs is None else qs
    scope = scope_of(user)
    if scope == DataScope.COMPANY:
        return qs.filter(Exists(WorkOrder.objects.filter(company_q(user.company), asset=OuterRef("pk"))))
    if scope == DataScope.DEPARTMENT:
        dept = _department(user)
        return qs.filter(department__name__iexact=dept) if dept else qs.none()
    return qs


def can_see_work_order(user, wo) -> bool:
    return not is_scoped(user) or work_orders(user, WorkOrder.objects.filter(pk=wo.pk)).exists()


def can_see_asset(user, asset) -> bool:
    return not is_scoped(user) or assets(user, Asset.objects.filter(pk=asset.pk)).exists()
