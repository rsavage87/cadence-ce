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

Access events (slice 20): every change to who may do what writes one AccessEvent in the same transaction as the change (the Users
and access tab's change log and the API's read them): an invitation (invite_user; a resend in invitations.send_invitation), a user's
role, company, or department (set_user_role, set_user_scope), deactivating and reactivating an account, adding a role, and a role's
level for a module or what it sees. `by` is who made the change (None for a command or the seed), `user` the account changed, `role`
the role involved, and `detail` says what changed in words, with labels rather than slugs ("Technician → CE manager", "Contracts:
View → Edit"), cut to fit its 300 characters. A refused change raises before anything is written, and a call that changes nothing
(the role, level, or scope it already has; deactivating an account that already is) writes nothing.

One person in several facilities (slice 22, apps.accounts.people): add_account, which invite_user and bootstrap_tenant share, gives
an address another facility's account uses (exactly as stored) a new account here for that same person, as a pending invitation
they join while signed in. The inviting facility cannot tell: its response, toast, status, event, change log, Users row, API row,
and Resend invite are the same as for an address nobody uses, and nothing is written in the other facility but the shared `person`.
"""
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils.text import slugify
from rest_framework.authtoken.models import Token

from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.equipment.models import Department, DeviceModel
from apps.tenants.context import get_current_tenant, tenant_context
from apps.workorders.models import WorkOrder
from apps.workorders.scoping import FIELD_SERVICE_SUFFIX, scope_of

from .models import DEFAULT_ROLES, DEFAULT_SCOPES, AccessEvent, DataScope, Level, Module, Role, User

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


def display_name(user) -> str:
    """A user's name in messages: the full name, else the email (as User.__str__), never a second facility's username first
    (slice 22: "<email>@<facility slug>" would say the address has an account elsewhere)."""
    return user.get_full_name() or user.email or user.username


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
    # By email after the name, never the username: someone who also works at another facility has a username of its own here
    # (slice 22), and the order must not tell them apart.
    qs = User.objects.filter(tenant=tenant).select_related("role").order_by("first_name", "last_name", "email", "pk")
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

def _exceeds(by, role) -> str:
    """The first module where `role` has more access than `by` (by its label), or "" when `by` has all of its access."""
    if by is None or by.is_superuser:
        return ""
    return next((Module(m).label for m in Module.values if role.level_for(m) > by.level_for(m)), "")


def _check_grant(by, role) -> None:
    """`by` may give `role` (invite, change a user's role, copy it, change what it sees, reactivate an account holding it) only if
    they have every module's access it has."""
    module = _exceeds(by, role)
    if module:
        raise ValidationError(f"You can give only a role whose access you have yourself: {role.name} has more {module} access than your role.")


def _check_outranks(by, user) -> None:
    """`by` may deactivate an account or change its role only if they have all the access the account's role has: a Users Full
    clerk manages the accounts below them, never a director's."""
    module = _exceeds(by, user.role) if user.role_id else ""
    if module:
        raise ValidationError(f"You can change only accounts whose access you have yourself: {display_name(user)} "
                              f"has more {module} access than your role.")


def _active_directors(tenant):
    """Accounts that hold the Director role (the system role) and can use it: active, and not an invitation still pending."""
    return User.objects.filter(tenant=tenant, is_active=True, role__is_system=True).exclude(PENDING_INVITE)


def _check_keeps_a_director(user) -> None:
    """A facility always keeps a director who can sign in: refuse to deactivate or move the last one. Run inside the caller's
    transaction: the facility's director rows are locked first, so two requests removing the last two directors at once cannot both
    see the other one still there. A pending invitation is not a director who can sign in, so withdrawing or moving one is never
    refused here."""
    if not (user.role_id and user.role.is_system and user.is_active) or (user.is_invited and user.last_login is None):
        return
    list(User.objects.select_for_update(of=("self",)).filter(tenant=user.tenant, is_active=True, role__is_system=True).order_by("pk")
         .values_list("pk", flat=True))
    if not _active_directors(user.tenant).exclude(pk=user.pk).exists():
        raise ValidationError(f"{display_name(user)} is this facility's only {user.role.name} who can sign in; give "
                              "someone else that role first.")


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


# --- access events (slice 20) ---------------------------------------------------------------------------------------------------

DETAIL_MAX = AccessEvent._meta.get_field("detail").max_length
BLANK = "—"  # an empty company or department in an event's words


def _inside(tenant):
    """Work inside `tenant` unless it is the current one already: a command, a test, or the signed-out password-reset form calls with
    none set, and row-level security accepts a row only while its own tenant is."""
    current = get_current_tenant()
    return nullcontext() if current is not None and current.pk == tenant.pk else tenant_context(tenant)


def _fit(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"


def record_access_event(tenant, action, *, by=None, user=None, role=None, role_id=None, detail="") -> AccessEvent:
    """One row in the facility's record of who may do what (AccessEvent), in the caller's transaction. `role`, or its id when only
    the id is at hand, is the role involved."""
    with _inside(tenant):
        event = AccessEvent(tenant=tenant, action=action, by=by, user=user, detail=_fit(detail))
        event.role_id = role.pk if role is not None else role_id
        event.save()
        return event


def _role_name(role) -> str:
    return role.name if role is not None else "No role"


def _arrow(before, after) -> str:
    return f"{before or BLANK} → {after or BLANK}"


def _unit_word(role) -> str:
    """A department-scoped role's users see their own unit; anyone else's department is where they work."""
    return "unit" if role is not None and role.effective_scope == DataScope.DEPARTMENT else "department"


def _scope_words(role, before: tuple[str, str], after: tuple[str, str]) -> list[str]:
    """"company: A → B" and "department: X → Y" (or "unit: ..."), for whichever of (company, department) changed."""
    words = []
    if before[0] != after[0]:
        words.append(f"company: {_arrow(before[0], after[0])}")
    if before[1] != after[1]:
        words.append(f"{_unit_word(role)}: {_arrow(before[1], after[1])}")
    return words


def _sentence(words: list[str]) -> str:
    text = "; ".join(words)
    return text[:1].upper() + text[1:]


def _is_pending(user) -> bool:
    return bool(user.is_invited and user.last_login is None)


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

UNUSABLE_ADDRESS = "That address cannot be used for a new account here. Contact support."


def normalize_email(email) -> str:
    """How an invited address is stored: trimmed and lowercased (the username of an account in one facility)."""
    return (email or "").strip().lower()


def is_member(tenant, email) -> bool:
    """Whether one of `tenant`'s own accounts already uses `email` as its username or address, in any letter case. Only this facility's
    accounts are read: an address used elsewhere is never confirmed to another facility."""
    return User.objects.filter(tenant=tenant).filter(Q(username__iexact=email) | Q(email__iexact=email)).exists()


def _linked_username(email: str, tenant) -> str:
    """A second facility's account needs a username of its own (usernames are unique across facilities): "<email>@<facility slug>",
    which has two @ signs, so it is never a valid address and never anyone's email. Too long for the column, a random one instead.
    Nobody types it: a sign-in by the email lands on the person's account used last (apps.accounts.backends)."""
    name = f"{email}@{tenant.slug}"
    return name if len(name) <= EMAIL_MAX else f"{uuid.uuid4().hex}@{tenant.slug}"


def _person_for(tenant, email: str):
    """The person a new account in `tenant` for `email` joins (slice 22, apps.accounts.people), or None for an account of its own.
    Other facilities' accounts whose stored email is exactly `email` (never in another letter case, never a username, never a
    superuser's), locked until the caller's transaction ends, so two facilities inviting the address at once link it once. They are
    one person when they all share one already, or when there is exactly one of them, unlinked: that account gets a new person,
    written only if it still has none (a concurrent link wins, and is read back). Several people, or several unlinked accounts
    under the address, name nobody for sure: None. Nothing else of the other facility's account is read or changed."""
    matches = list(User.objects.select_for_update().filter(email=email, tenant__isnull=False, is_superuser=False).exclude(tenant=tenant)
                   .order_by("pk").values_list("pk", "person"))
    persons = {person for _pk, person in matches}
    if len(persons) == 1 and None not in persons:
        return persons.pop()
    if len(matches) == 1:
        pk = matches[0][0]
        User.objects.filter(pk=pk, person__isnull=True).update(person=uuid.uuid4())
        return User.objects.filter(pk=pk).values_list("person", flat=True).get()
    return None


@transaction.atomic
def add_account(tenant, *, email, role, first_name="", last_name="", department="", company="", is_staff=False) -> User:
    """A new pending invitation in `tenant` (no usable password): invite_user's account and bootstrap_tenant's first director. In
    the caller's transaction when there is one (the other facilities' rows stay locked until it ends); `email` is stored as
    normalize_email gives it, and the caller has checked is_member.

    Slice 22: when the address is another facility's account's (see _person_for), the new account is that person's account here
    (their one password, their sign-in), with a username of its own; it never reuses or moves the other facility's account. It
    looks exactly like any other invitation to this facility: the same status, events, and rows. A clash nobody can resolve here
    (a username already taken by an account with another address, a person who already has an account here under another
    address) is today's refusal, which says nothing about why."""
    email = normalize_email(email)
    person = _person_for(tenant, email)
    user = User(username=_linked_username(email, tenant) if person else email, email=email, person=person, first_name=first_name,
                last_name=last_name, tenant=tenant, role=role, department=department, company=company, is_invited=True, is_active=True,
                is_staff=is_staff)
    user.set_unusable_password()
    try:
        # savepoint: a concurrent invite for the same email would otherwise poison the outer transaction
        with transaction.atomic():
            user.save()
    except IntegrityError:
        # username is unique across facilities, and a person has one account per facility; say only that the address cannot be used here
        raise ValidationError(UNUSABLE_ADDRESS)
    return user


@transaction.atomic
def invite_user(tenant, *, email, first_name, last_name, role, department="", company="", create_technician=False, by=None) -> User:
    """Create an Invited account with no usable password (add_account: an address another facility's account uses joins that
    person, and nothing here says so). The caller then sends the invitation email (accounts.invitations.send_invitation) outside
    this transaction, so a mail failure never undoes the account. A company-scoped role's user needs the company they work for, and
    a department-scoped role's user one of the facility's departments."""
    email = normalize_email(email)
    first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
    if not email:
        raise ValidationError("A work email is required.")
    if len(email) > EMAIL_MAX:  # it becomes the username, which holds no more
        raise ValidationError(f"Keep the email to {EMAIL_MAX} characters.")
    if not first_name or not last_name:
        raise ValidationError("First and last name are required.")
    _check_role(tenant, role)
    _check_grant(by, role)
    if is_member(tenant, email):
        raise ValidationError(f"{email} is already a member of this facility.")
    company, department = _scoped_values(tenant, role, _clean_company(company), (department or "").strip())
    user = add_account(tenant, email=email, first_name=first_name, last_name=last_name, role=role, department=department, company=company)
    if create_technician:
        Technician.objects.create(tenant=tenant, user=user, name=f"{first_name} {last_name}"[:120], title=NEW_TECHNICIAN_TITLE)  # its column
    words = [f"Invited as {role.name}"] + [f"{label}: {value}" for label, value in (("company", company), (_unit_word(role), department)) if value]
    if create_technician:
        words.append("technician profile added")
    record_access_event(tenant, AccessEvent.Action.INVITED, by=by, user=user, role=role, detail="; ".join(words))
    return user


def _new_values(user, company, department) -> tuple[str, str]:
    """What the account's company and department become: None keeps what it has."""
    new_company = user.company if company is None else _clean_company(company)
    new_department = (user.department if department is None else (department or "")).strip()
    return new_company, new_department


@transaction.atomic
def set_user_role(user, role, *, company=None, department=None, by=None) -> User:
    """Give `user` another role. A company- or department-scoped role needs the user's company or department: the one on the
    account, or `company` / `department` given here (None keeps the account's), checked and saved with the role in one change."""
    _check_role(user.tenant, role)
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot change your own role.")
    _check_grant(by, role)
    if role.pk != user.role_id:
        _check_outranks(by, user)
        _check_keeps_a_director(user)
    company, department = _scoped_values(user.tenant, role, *_new_values(user, company, department))
    old_role = user.role if user.role_id else None
    before = {"role": old_role.slug if old_role else "", "company": user.company, "department": user.department}
    user.role, user.company, user.department = role, company, department
    user.save(update_fields=["role", "company", "department"])
    _audit(user, by, {"role": (before["role"], role.slug), "company": (before["company"], company), "department": (before["department"], department)})
    scope_words = _scope_words(role, (before["company"], before["department"]), (company, department))
    if old_role is None or old_role.pk != role.pk:
        record_access_event(user.tenant, AccessEvent.Action.ROLE_CHANGED, by=by, user=user, role=role,
                            detail="; ".join([_arrow(_role_name(old_role), role.name), *scope_words]))
    elif scope_words:  # the role it had, with another company or department
        record_access_event(user.tenant, AccessEvent.Action.SCOPE_CHANGED, by=by, user=user, role=role, detail=_sentence(scope_words))
    return user


@transaction.atomic
def set_user_scope(user, *, company=None, department=None, by=None) -> User:
    """Set or change the company a user works for and the department they belong to (None keeps the account's), checked against
    what their role's scope needs. A scoped user cannot change their own: it decides what they see."""
    company, department = _new_values(user, company, department)
    if by is not None and by.pk == user.pk and scope_of(user) != DataScope.FACILITY and (company, department) != (user.company, user.department):
        raise ValidationError("You cannot change your own company or department: they decide what you see.")
    role = user.role if user.role_id else None
    company, department = _scoped_values(user.tenant, role, company, department)
    if (company, department) == (user.company, user.department):
        return user
    before = (user.company, user.department)
    user.company, user.department = company, department
    user.save(update_fields=["company", "department"])
    _audit(user, by, {"company": (before[0], company), "department": (before[1], department)})
    record_access_event(user.tenant, AccessEvent.Action.SCOPE_CHANGED, by=by, user=user, role=role,
                        detail=_sentence(_scope_words(role, before, (company, department))))
    return user


def set_user_access(user, *, role, company=None, department=None, by=None) -> User:
    """The Users tab's Edit: the role, company, and department in one change, so a move to a scoped role brings what it needs."""
    if role is not None and role.pk != user.role_id:
        return set_user_role(user, role, company=company, department=department, by=by)
    return set_user_scope(user, company=company, department=department, by=by)


@transaction.atomic
def deactivate_user(user, by=None) -> User:
    if by is not None and by.pk == user.pk:
        raise ValidationError("You cannot deactivate your own account.")
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    _check_outranks(by, user)
    _check_keeps_a_director(user)
    was_active, pending = user.is_active, _is_pending(user)
    user.is_active = False
    fields = ["is_active"]
    if not user.has_usable_password():
        # A withdrawn invitation stays withdrawn: a fresh unusable password changes what invitation links hash, so an
        # earlier link does not come back to life if the account is reactivated (Resend invite sends a new one).
        user.set_unusable_password()
        fields.append("password")
    user.save(update_fields=fields)
    # Their API tokens go too: token sign-in refuses an inactive user, but reactivating must not bring an old token back to life.
    tokens, _ = Token.objects.filter(user=user).delete()
    if was_active:
        role = user.role if user.role_id else None
        words = [f"Invitation as {_role_name(role)} withdrawn; its link stops working" if pending else f"{_role_name(role)}; can no longer sign in"]
        if tokens:
            words.append("API token removed")
        record_access_event(user.tenant, AccessEvent.Action.DEACTIVATED, by=by, user=user, role=role, detail="; ".join(words))
    return user


@transaction.atomic
def reactivate_user(user, by=None) -> User:
    if user.is_superuser:
        raise ValidationError("Superusers are managed in Admin.")
    if user.role_id:
        _check_grant(by, user.role)  # bringing back an account gives its role again
    was_active = user.is_active
    user.is_active = True
    user.save(update_fields=["is_active"])
    if not was_active:
        role = user.role if user.role_id else None
        detail = (f"Invitation as {_role_name(role)} open again; Resend invite sends a new link" if _is_pending(user) else
                  f"{_role_name(role)}; can sign in again")
        record_access_event(user.tenant, AccessEvent.Action.REACTIVATED, by=by, user=user, role=role, detail=detail)
    return user


# --- roles ------------------------------------------------------------------------------------------------------------------

@transaction.atomic
def set_role_level(role, module, level, by=None) -> Role:
    if module not in Module.values:
        raise ValidationError("Unknown module.")
    if level not in Level.values:
        raise ValidationError("Unknown permission level.")
    if role.is_system:
        raise ValidationError(f"The {role.name} role is fixed and cannot be changed.")
    if by is not None and by.role_id == role.pk:
        raise ValidationError("You cannot change the permissions of your own role.")
    before = role.level_for(module)
    if by is not None and not by.is_superuser and level > before and level > by.level_for(module):
        raise ValidationError(f"You cannot give a role more {Module(module).label} access than your own role has.")
    role.set_levels({module: level})
    if level != before:
        record_access_event(role.tenant, AccessEvent.Action.ROLE_LEVEL_CHANGED, by=by, role=role,
                            detail=f"{Module(module).label}: {_arrow(Level(before).label, Level(level).label)}")
    return role


def scope_fixed_reason(role) -> str:
    """Why `role`'s scope cannot change, or "": the Director always sees the whole facility, and the default vendor and requester
    roles keep the narrow view their descriptions promise (DEFAULT_SCOPES)."""
    if role.is_system:
        return f"The {role.name} role is fixed and cannot be changed."
    if role.slug in DEFAULT_SCOPES:
        return f"The {role.name} role always sees {DataScope(DEFAULT_SCOPES[role.slug]).label.lower()}, as its description says."
    return ""


@transaction.atomic
def set_role_scope(role, scope, by=None) -> Role:
    """Which devices and work orders a role's users see (audited in Role.history, and an access event when what its users see
    changes). Narrowing a role whose users have no company or department yet is allowed (closed, never open): they see nothing until
    one is set, and users_without_scope says who."""
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
        _check_grant(by, role)  # what a role sees is part of the access it gives
        before = role.effective_scope
        role.scope = scope
        role._history_user = by
        role.save(update_fields=["scope", "updated_at"])
        if role.effective_scope != before:  # a blank scope set to the default it already took changes what nobody sees
            record_access_event(role.tenant, AccessEvent.Action.ROLE_SCOPE_CHANGED, by=by, role=role,
                                detail=_arrow(DataScope(before).label, DataScope(role.effective_scope).label))
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
    sees = DataScope(scope).label
    record_access_event(tenant, AccessEvent.Action.ROLE_CREATED, by=by, role=role,
                        detail=f"Copied from {copy_from.name}; sees {sees[:1].lower()}{sees[1:]}")
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
