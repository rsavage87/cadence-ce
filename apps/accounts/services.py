"""
User and role administration. The Users and access screen calls these; views never set fields on User or Role themselves.

User is not a TenantModel, so every query here filters on an explicit tenant. Rules raise ValidationError with the
message the UI shows in its toast.
"""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils.text import slugify

from apps.credentials.models import Technician

from .models import DEFAULT_ROLES, Level, Module, Role, User

# Status is derived, not stored: an invited account that has signed in is simply active.
USER_STATUSES = [("active", "Active"), ("invited", "Invited"), ("deactivated", "Deactivated")]
PENDING_INVITE = Q(is_invited=True, last_login__isnull=True)
NEW_TECHNICIAN_TITLE = "BMET I"


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
        qs = qs.filter(Q(first_name__icontains=f.q) | Q(last_name__icontains=f.q) | Q(email__icontains=f.q) | Q(department__icontains=f.q))
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


@transaction.atomic
def invite_user(tenant, *, email, first_name, last_name, role, department="", create_technician=False, by=None) -> User:
    """Create an Invited account with no usable password. The caller then sends the invitation email
    (accounts.invitations.send_invitation) outside this transaction, so a mail failure never undoes the account."""
    email = (email or "").strip().lower()
    first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
    if not email:
        raise ValidationError("A work email is required.")
    if not first_name or not last_name:
        raise ValidationError("First and last name are required.")
    _check_role(tenant, role)
    # Only this facility's accounts are named; an address used elsewhere must not be confirmed to another tenant.
    if User.objects.filter(tenant=tenant).filter(Q(username__iexact=email) | Q(email__iexact=email)).exists():
        raise ValidationError(f"{email} is already a member of this facility.")
    user = User(username=email, email=email, first_name=first_name, last_name=last_name, tenant=tenant, role=role, department=(department or "").strip()[:80],
                is_invited=True, is_active=True)
    user.set_unusable_password()
    try:
        # savepoint: a concurrent invite for the same email would otherwise poison the outer transaction
        with transaction.atomic():
            user.save()
    except IntegrityError:
        # username is unique across tenants; say only that the address cannot be used here
        raise ValidationError("That address cannot be used for a new account here. Contact support.")
    if create_technician:
        Technician.objects.create(tenant=tenant, user=user, name=f"{first_name} {last_name}", title=NEW_TECHNICIAN_TITLE)
    return user


def set_user_role(user, role, by=None) -> User:
    _check_role(user.tenant, role)
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot change your own role.")
    user.role = role
    user.save(update_fields=["role"])
    return user


def deactivate_user(user, by=None) -> User:
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot deactivate your own account.")
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    user.is_active = False
    user.save(update_fields=["is_active"])
    return user


def reactivate_user(user, by=None) -> User:
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    user.is_active = True
    user.save(update_fields=["is_active"])
    return user


def set_role_level(role, module, level, by=None) -> Role:
    if module not in Module.values:
        raise ValidationError("Unknown module.")
    if level not in Level.values:
        raise ValidationError("Unknown permission level.")
    if role.is_system:
        raise ValidationError(f"The {role.name} role is fixed and cannot be changed.")
    if by is not None and by.role_id == role.pk:
        raise ValidationError("You cannot change the permissions of your own role.")
    role.set_levels({module: level})
    return role


@transaction.atomic
def create_role(*, name, description="", copy_from: Role) -> Role:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the role a name.")
    tenant = copy_from.tenant
    description = (description or "").strip() or f"Copied from {copy_from.name}"
    role = Role(tenant=tenant, name=name[:80], slug=_unique_slug(tenant, name), description=description[:300])
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
