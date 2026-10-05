"""
User and role administration. The Users and access screen calls these; views never set fields on User or Role themselves.

User is not a TenantModel, so every query here filters on an explicit tenant. Rules raise ValidationError with the
message the UI shows in its toast (keyed by "company", "department", or "scope" when the message is about that field).

Data scope (slice 16; apps.workorders.scoping applies it): a company-scoped role's users see only the work orders assigned to the
company on their account, and a department-scoped role's users only their own unit's devices and those devices' work orders. So a
user gets the company, or a department that is one of the facility's Departments, together with such a role (invite_user,
set_user_role) and keeps one while they hold it (set_user_scope). A scoped user without one (an account from before slice 16, a
role narrowed since, a department renamed since) sees nothing, and the Users list says so (scope_gap). The Director always sees
the whole facility, and the default vendor and requester roles keep the narrow view their descriptions promise.

Audit: a role's scope is in Role.history. User has no history table, so a change to a user's role, company, or department is
written to the "cadence.audit" log, one line per field.
"""
import logging
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils.text import slugify
from rest_framework.authtoken.models import Token

from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.equipment.models import Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder
from apps.workorders.scoping import FIELD_SERVICE_SUFFIX, scope_of

from .models import DEFAULT_ROLES, DEFAULT_SCOPES, DataScope, Level, Module, Role, User

audit_log = logging.getLogger("cadence.audit")

# Status is derived, not stored: an invited account that has signed in is simply active.
USER_STATUSES = [("active", "Active"), ("invited", "Invited"), ("deactivated", "Deactivated")]
PENDING_INVITE = Q(is_invited=True, last_login__isnull=True)
EMAIL_MAX = User._meta.get_field("username").max_length  # an invited user's email is their username
NEW_TECHNICIAN_TITLE = "BMET I"
COMPANY_MAX_LENGTH = 120  # User.company
DEPARTMENT_MAX_LENGTH = 80  # User.department
SCOPE_CHOICE_MESSAGE = "Choose who the role sees: the whole facility, their company's work orders, or their department's."



# Departments Invite user and Edit suggest besides the tenant's clinical departments (the mock's list); the API takes the same.
STANDING_DEPARTMENTS = (["Clinical Engineering"], ["Finance", "Quality and Patient Safety", "External vendor"])


def facility_departments() -> list[str]:
    return list(Department.objects.values_list("name", flat=True))


def department_options() -> list[str]:
    """The units a user can be put in: the standing ones and the facility's departments, each once."""
    before, after = STANDING_DEPARTMENTS
    names = before + facility_departments() + after
    return list(dict.fromkeys(names))

def count_active_users(tenant) -> int:
    """Active accounts that have signed in at least once; invited ones are not counted, matching the mock's status chips."""
    return User.objects.filter(tenant=tenant, is_active=True).exclude(PENDING_INVITE).count()


_DEFAULT_ROLE_RANK = {slug: i for i, (slug, *_rest) in enumerate(DEFAULT_ROLES)}


def role_order(roles) -> list:
    """The mock's order: the standard roles as DEFAULT_ROLES lists them (Director first), then custom roles by name."""
    return sorted(roles, key=lambda r: (_DEFAULT_ROLE_RANK.get(r.slug, len(_DEFAULT_ROLE_RANK)), r.name))


def user_status(user) -> str:
    if not user.is_active:
        return "deactivated"
    if user.is_invited and user.last_login is None:
        return "invited"
    return "active"


@dataclass
class UserFilters:
    q: str = ""
    role: str = ""     # role slug
    status: str = ""   # one of USER_STATUSES


def list_users(tenant, f: UserFilters):
    qs = User.objects.filter(tenant=tenant).select_related("role").order_by("first_name", "last_name", "username")
    if f.q:
        qs = qs.filter(Q(first_name__icontains=f.q) | Q(last_name__icontains=f.q) | Q(email__icontains=f.q) | Q(department__icontains=f.q)
                       | Q(company__icontains=f.q))
    if f.role:
        qs = qs.filter(role__slug=f.role)
    if f.status == "deactivated":
        qs = qs.filter(is_active=False)
    elif f.status == "invited":
        qs = qs.filter(is_active=True).filter(PENDING_INVITE)
    elif f.status == "active":
        qs = qs.filter(is_active=True).exclude(PENDING_INVITE)
    return qs


def role_user_counts(tenant) -> dict:
    """{role_id: active users holding the role}. Invited users count; deactivated ones do not, matching the mock."""
    rows = User.objects.filter(tenant=tenant, is_active=True, role__isnull=False).values("role_id").annotate(n=Count("id"))
    return {r["role_id"]: r["n"] for r in rows}


def _check_role(tenant, role):
    if role is None or role.tenant_id != tenant.id:
        raise ValidationError("Choose a role from this facility.")


# --- nobody hands out more access than they have, and a facility keeps a director (slice 19) ------------------------------------
# The Users and Roles tabs and the API (apps/api/views_users.py) share these: a custom role with Users Full can manage accounts,
# but only up to its own access, so it can never make itself or anyone a director, nor copy or raise a role past its own levels.

def _check_grant(by, role) -> None:
    """`by` may give `role` (invite, change a user's role, copy it into a new role) only if they have every module's access it has."""
    if by is None or by.is_superuser:
        return
    for module in Module.values:
        if role.level_for(module) > by.level_for(module):
            raise ValidationError(f"You can give only a role whose access you have yourself: {role.name} has more "
                                  f"{Module(module).label} access than your role.")


def _active_directors(tenant):
    """Accounts that hold the Director role (the system role) and can use it: active, and not an invitation still pending."""
    return User.objects.filter(tenant=tenant, is_active=True, role__is_system=True).exclude(PENDING_INVITE)


def _check_keeps_a_director(user) -> None:
    """A facility always keeps a director who can sign in: refuse to deactivate or move the last one."""
    if user.role_id and user.role.is_system and user.is_active and not _active_directors(user.tenant).exclude(pk=user.pk).exists():
        raise ValidationError(f"{user.get_full_name() or user.username} is this facility's only {user.role.name}; give someone else that "
                              "role first.")


# --- data scope: the company or department a scoped role's user needs ----------------------------------------------------------

def _clean_company(company) -> str:
    """Trimmed at the ends, and otherwise exactly as typed: scoping matches the whole name, in any letter case, against the vendor
    name stored on work orders (a contract's vendor as entered), so collapsing a double space here would make it unmatchable."""
    company = str(company or "").strip()
    if len(company) > COMPANY_MAX_LENGTH:
        raise ValidationError({"company": f"Keep the company name to {COMPANY_MAX_LENGTH} characters."})
    return company


# The reads below look at the user's facility (inside it, so they see its rows under row-level security whoever calls them).

def facility_department(tenant, name) -> Department | None:
    """The facility's Department named `name` in any letter case, as scoping matches a department-scoped user's unit."""
    name = (name or "").strip()
    if not name:
        return None
    with tenant_context(tenant):
        return Department.objects.filter(tenant=tenant, name__iexact=name).first()


def department_names(tenant) -> set[str]:
    """The facility's Department names upper-cased, for scope_gap over a list of users (one query, not one per row)."""
    with tenant_context(tenant):
        return {n.upper() for n in Department.objects.filter(tenant=tenant).values_list("name", flat=True)}


def _scoped_values(tenant, role, company: str, department: str) -> tuple[str, str]:
    """The company and department a user of `role` ends up with, or ValidationError keyed by the field `role`'s scope is missing.
    A department-scoped user's department must be one of the facility's Departments (stored as the facility names it): the
    standing names the invite form also offers (Clinical Engineering, Finance, External vendor) have no devices, so they are refused."""
    scope = role.effective_scope if role is not None else DataScope.FACILITY
    if scope == DataScope.COMPANY and not company:
        raise ValidationError({"company": f"Enter the company this user works for: the {role.name} role sees only the work orders assigned to it."})
    if scope != DataScope.DEPARTMENT:
        return company, department[:DEPARTMENT_MAX_LENGTH]
    if not department:
        raise ValidationError({"department": f"Choose the unit this user works in: the {role.name} role sees only its own department's devices "
                                             "and work orders."})
    match = facility_department(tenant, department)
    if match is None:
        raise ValidationError({"department": f"{department} is not one of this facility's departments. The {role.name} role sees only its own "
                                             "department's devices and work orders: choose the unit this user works in."})
    if len(match.name) > DEPARTMENT_MAX_LENGTH:
        raise ValidationError({"department": f"{match.name} is too long a name for an account ({DEPARTMENT_MAX_LENGTH} characters at most): "
                                             "shorten the department's name first."})
    return company, match.name


def _audit(user, by, changes: dict):
    """One "cadence.audit" line per field that changed: {field: (before, after)}."""
    for field, (before, after) in changes.items():
        if before != after:
            audit_log.info("user %s (tenant %s): %s changed from %r to %r by user %s", user.pk, user.tenant_id, field, before, after,
                           getattr(by, "pk", None))


def scope_gap(user, departments: set[str] | None = None) -> str:
    """What keeps a scoped user from seeing anything: "company" or "department" when it is missing (or names none of the facility's
    departments any more), "" when nothing is. `departments` is department_names(), when the caller already has it."""
    scope = scope_of(user)
    if scope == DataScope.COMPANY:
        return "" if (user.company or "").strip() else "company"
    if scope == DataScope.DEPARTMENT:
        name = (user.department or "").strip()
        if not name:
            return "department"
        known = name.upper() in departments if departments is not None else facility_department(user.tenant, name) is not None
        return "" if known else "department"
    return ""


def company_suggestions(tenant) -> list[str]:
    """The companies a company-scoped user may work for, named as the facility's work orders name vendors: its contracts' vendors,
    "<manufacturer> field service" for its device models' makers (web.forms.vendor_name_for), and any other vendor already on a work
    order, each once in any letter case (spelled as the first of those names it). "<X> field service" is left out where X itself is
    suggested: a user of company X sees both. Any other typed name is allowed."""
    with tenant_context(tenant):
        names = list(Contract.objects.filter(tenant=tenant).order_by("vendor").values_list("vendor", flat=True))
        makers = DeviceModel.objects.filter(tenant=tenant).order_by("manufacturer").values_list("manufacturer", flat=True).distinct()
        names += [m + FIELD_SERVICE_SUFFIX for m in makers]
        names += (WorkOrder.objects.filter(tenant=tenant, vendor_service=True).order_by("vendor_name").values_list("vendor_name", flat=True)
                  .distinct())
    by_key = {}
    for name in names:
        name = (name or "").strip()  # as stored, inner spacing kept (scoping matches it exactly, any case)
        if name:
            by_key.setdefault(name.upper(), name)
    suffix = FIELD_SERVICE_SUFFIX.upper()
    kept = [name for key, name in by_key.items() if not (key.endswith(suffix) and key[: -len(suffix)] in by_key)]
    return sorted(kept, key=str.upper)


# --- users ------------------------------------------------------------------------------------------------------------------

@transaction.atomic
def invite_user(tenant, *, email, first_name, last_name, role, department="", company="", create_technician=False, by=None) -> User:
    """Create an Invited account with no usable password. The caller then sends the invitation email
    (accounts.invitations.send_invitation) outside this transaction, so a mail failure never undoes the account. A company-scoped
    role's user needs the company they work for, and a department-scoped role's user one of the facility's departments."""
    email = (email or "").strip().lower()
    first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
    if not email:
        raise ValidationError("A work email is required.")
    if len(email) > EMAIL_MAX:  # it becomes the username, which holds no more
        raise ValidationError(f"Keep the email to {EMAIL_MAX} characters.")
    if not first_name or not last_name:
        raise ValidationError("First and last name are required.")
    _check_role(tenant, role)
    _check_grant(by, role)
    # Only this facility's accounts are named; an address used elsewhere must not be confirmed to another tenant.
    if User.objects.filter(tenant=tenant).filter(Q(username__iexact=email) | Q(email__iexact=email)).exists():
        raise ValidationError(f"{email} is already a member of this facility.")
    company, department = _scoped_values(tenant, role, _clean_company(company), (department or "").strip())
    user = User(username=email, email=email, first_name=first_name, last_name=last_name, tenant=tenant, role=role, department=department,
                company=company, is_invited=True, is_active=True)
    user.set_unusable_password()
    try:
        # savepoint: a concurrent invite for the same email would otherwise poison the outer transaction
        with transaction.atomic():
            user.save()
    except IntegrityError:
        # username is unique across tenants; say only that the address cannot be used here
        raise ValidationError("That address cannot be used for a new account here. Contact support.")
    if create_technician:
        Technician.objects.create(tenant=tenant, user=user, name=f"{first_name} {last_name}"[:120], title=NEW_TECHNICIAN_TITLE)  # its column
    return user


def _new_values(user, company, department) -> tuple[str, str]:
    """What the account's company and department become: None keeps what it has."""
    new_company = user.company if company is None else _clean_company(company)
    new_department = (user.department if department is None else (department or "")).strip()
    return new_company, new_department


def set_user_role(user, role, *, company=None, department=None, by=None) -> User:
    """Give `user` another role. A company- or department-scoped role needs the user's company or department: the one on the
    account, or `company` / `department` given here (None keeps the account's), checked and saved with the role in one change."""
    _check_role(user.tenant, role)
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot change your own role.")
    _check_grant(by, role)
    if role.pk != user.role_id:
        _check_keeps_a_director(user)
    company, department = _scoped_values(user.tenant, role, *_new_values(user, company, department))
    before = {"role": user.role.slug if user.role_id else "", "company": user.company, "department": user.department}
    user.role, user.company, user.department = role, company, department
    user.save(update_fields=["role", "company", "department"])
    _audit(user, by, {"role": (before["role"], role.slug), "company": (before["company"], company), "department": (before["department"], department)})
    return user


def set_user_scope(user, *, company=None, department=None, by=None) -> User:
    """Set or change the company a user works for and the department they belong to (None keeps the account's), checked against
    what their role's scope needs. A scoped user cannot change their own: it decides what they see."""
    company, department = _new_values(user, company, department)
    if by is not None and by.pk == user.pk and scope_of(user) != DataScope.FACILITY and (company, department) != (user.company, user.department):
        raise ValidationError("You cannot change your own company or department: they decide what you see.")
    company, department = _scoped_values(user.tenant, user.role if user.role_id else None, company, department)
    if (company, department) == (user.company, user.department):
        return user
    before = (user.company, user.department)
    user.company, user.department = company, department
    user.save(update_fields=["company", "department"])
    _audit(user, by, {"company": (before[0], company), "department": (before[1], department)})
    return user


def set_user_access(user, *, role, company=None, department=None, by=None) -> User:
    """The Users tab's Edit: the role, company, and department in one change, so a move to a scoped role brings what it needs."""
    if role is not None and role.pk != user.role_id:
        return set_user_role(user, role, company=company, department=department, by=by)
    return set_user_scope(user, company=company, department=department, by=by)


def deactivate_user(user, by=None) -> User:
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot deactivate your own account.")
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    _check_keeps_a_director(user)
    user.is_active = False
    fields = ["is_active"]
    if not user.has_usable_password():
        # A withdrawn invitation stays withdrawn: a fresh unusable password changes what invitation links hash, so an
        # earlier link does not come back to life if the account is reactivated (Resend invite sends a new one).
        user.set_unusable_password()
        fields.append("password")
    user.save(update_fields=fields)
    # Their API tokens go too: token sign-in refuses an inactive user, but reactivating must not bring an old token back to life.
    Token.objects.filter(user=user).delete()
    return user


def reactivate_user(user, by=None) -> User:
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    user.is_active = True
    user.save(update_fields=["is_active"])
    return user


# --- roles ------------------------------------------------------------------------------------------------------------------

def set_role_level(role, module, level, by=None) -> Role:
    if module not in Module.values:
        raise ValidationError("Unknown module.")
    if level not in Level.values:
        raise ValidationError("Unknown permission level.")
    if role.is_system:
        raise ValidationError(f"The {role.name} role is fixed and cannot be changed.")
    if by is not None and by.role_id == role.pk:
        raise ValidationError("You cannot change the permissions of your own role.")
    if by is not None and not by.is_superuser and level > role.level_for(module) and level > by.level_for(module):
        raise ValidationError(f"You cannot give a role more {Module(module).label} access than your own role has.")
    role.set_levels({module: level})
    return role


def scope_fixed_reason(role) -> str:
    """Why `role`'s scope cannot change, or "": the Director always sees the whole facility, and the default vendor and requester
    roles keep the narrow view their descriptions promise (DEFAULT_SCOPES)."""
    if role.is_system:
        return f"The {role.name} role is fixed and cannot be changed."
    if role.slug in DEFAULT_SCOPES:
        return f"The {role.name} role always sees {DataScope(DEFAULT_SCOPES[role.slug]).label.lower()}, as its description says."
    return ""


def set_role_scope(role, scope, by=None) -> Role:
    """Which devices and work orders a role's users see (audited in Role.history). Narrowing a role whose users have no company or
    department yet is allowed (closed, never open): they see nothing until one is set, and users_without_scope says who."""
    if scope not in DataScope.values:
        raise ValidationError(SCOPE_CHOICE_MESSAGE)
    reason = scope_fixed_reason(role)
    if reason:
        if scope != role.effective_scope:
            raise ValidationError(reason)
        return role  # already what it always is; a fixed role is never written
    if by is not None and by.role_id == role.pk:
        raise ValidationError("You cannot change what your own role sees.")
    if role.scope != scope:
        role.scope = scope
        role._history_user = by
        role.save(update_fields=["scope", "updated_at"])
    return role


def users_without_scope(role) -> list:
    """The active users of `role` who see nothing because the company or department its scope needs is missing."""
    users = User.objects.filter(tenant=role.tenant, role=role, is_active=True).select_related("role")
    names = department_names(role.tenant)
    return [u for u in users if scope_gap(u, names)]


@transaction.atomic
def create_role(*, name, description="", copy_from: Role, scope=DataScope.FACILITY, by=None) -> Role:
    """A custom role with `copy_from`'s levels and the scope chosen for it (the whole facility unless said otherwise), stored on
    the role, so the slug defaults (DEFAULT_SCOPES) never reach a custom role."""
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the role a name.")
    if scope not in DataScope.values:
        raise ValidationError({"scope": SCOPE_CHOICE_MESSAGE})
    _check_grant(by, copy_from)
    tenant = copy_from.tenant
    description = (description or "").strip() or f"Copied from {copy_from.name}"
    role = Role(tenant=tenant, name=name[:80], slug=_unique_slug(tenant, name), description=description[:300], scope=scope)
    role._history_user = by
    role.save()
    role.set_levels({m: copy_from.level_for(m) for m in Module.values})
    return role


def _unique_slug(tenant, name: str) -> str:
    base = slugify(name)[:40] or "role"
    # unscoped with an explicit tenant: the slug is unique per tenant whether or not a request context is set
    taken = set(Role.unscoped.filter(tenant=tenant, slug__startswith=base[:36]).values_list("slug", flat=True))
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        slug = f"{base[:40 - len(suffix)]}{suffix}"
        if slug not in taken:
            return slug
        n += 1
