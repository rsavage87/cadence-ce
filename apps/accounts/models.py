from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.tenants.context import tenant_context


class Module(models.TextChoices):
    EQUIPMENT = "equipment", "Equipment"
    WORKORDERS = "workorders", "Work orders"
    PM = "pm", "PM schedule"
    CONTRACTS = "contracts", "Contracts"
    RECALLS = "recalls", "Recalls"
    REPORTS = "reports", "Reports"
    USERS = "users", "Users and access"
    SETTINGS = "settings", "Settings"
    INCIDENTS = "incidents", "Incidents"  # slice 28: device incidents (apps.incidents)


class Level(models.IntegerChoices):
    NONE = 0, "None"
    VIEW = 1, "View"
    REQUEST = 2, "Request"
    EDIT = 3, "Edit"
    APPROVE = 4, "Approve"
    FULL = 5, "Full"


class DataScope(models.TextChoices):
    """Which devices and work orders a role's users see inside their facility (slice 16; apps.workorders.scoping applies it)."""
    FACILITY = "facility", "The whole facility"
    COMPANY = "company", "Only work orders assigned to their company"
    DEPARTMENT = "department", "Only their department's devices and work orders"


# A role whose scope is blank takes its default by slug: the vendor technician sees only work orders assigned to their company and
# the clinical requester only their own unit's (the default roles' descriptions); every other role sees the whole facility.
DEFAULT_SCOPES = {"vendor": DataScope.COMPANY, "requester": DataScope.DEPARTMENT}


class Role(TenantModel):
    """A named access level. Permissions are one row per module (see RolePermission)."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=40)
    description = models.CharField(max_length=300, blank=True)
    is_system = models.BooleanField(default=False, help_text="System roles (e.g. Director) cannot be edited or deleted.")
    scope = models.CharField(max_length=20, choices=DataScope.choices, blank=True,
                             help_text="Which devices and work orders its users see; blank takes the default for the role (DEFAULT_SCOPES)")
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "slug"], name="uniq_role_slug_per_tenant")]

    def __str__(self):
        return self.name

    @property
    def effective_scope(self) -> str:
        return self.scope or DEFAULT_SCOPES.get(self.slug, DataScope.FACILITY)

    def levels(self) -> dict:
        """{module: level} for every module. A module the role has no row for (one added after the role was made: Incidents, slice
        28) takes FULL on a system role, its default level on a default role (DEFAULT_LEVELS, by slug), and None on a custom role; a row
        saved as None stays None. Every reader of a role's levels goes through this (level_for, the role matrix, the API's role
        serializer and its PATCH), so the matrix shows the level in force and the "never more than your own" check reads the same."""
        held = {p.module: p.level for p in self.permissions.all()}
        defaults = DEFAULT_LEVELS.get(self.slug, {})
        return {m: held[m] if m in held else (Level.FULL if self.is_system else defaults.get(m, Level.NONE)) for m in Module.values}

    def level_for(self, module: str) -> int:
        return self.levels().get(module, Level.NONE)

    def set_levels(self, levels: dict):
        for module, level in levels.items():
            RolePermission.unscoped.update_or_create(tenant=self.tenant, role=self, module=module, defaults={"level": level})


class RolePermission(TenantModel):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    module = models.CharField(max_length=20, choices=Module.choices)
    level = models.PositiveSmallIntegerField(choices=Level.choices, default=Level.NONE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["role", "module"], name="uniq_role_module")]

    def __str__(self):
        return f"{self.role}: {self.get_module_display()} = {self.get_level_display()}"


class User(AbstractUser):
    """Users belong to exactly one tenant. Superusers may have no tenant and switch in the admin.

    Slice 22, one person in several facilities: a person keeps one account per facility (its role, company, unit, API tokens,
    preferences, and history stay that facility's), and the accounts that share `person` are the same person (apps.accounts.people).
    They share one password: a password set on one (the change form, a reset, an invitation, Admin, a hasher upgrade) is written to
    the person's other accounts that have a usable one, in the same transaction, and an invitation still pending elsewhere gets a
    fresh unusable password so its emailed link stops working. Only an invitation links accounts, on the exact stored email."""

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, null=True, blank=True, related_name="users")
    person = models.UUIDField(null=True, blank=True, editable=False,
                              help_text="Accounts of one person in several facilities share this; empty for an account in one facility.")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")
    department = models.CharField(max_length=80, blank=True)
    # Slice 16: the vendor a company-scoped user works for, as work orders name it (WorkOrder.vendor_name: a contract's vendor, or
    # "<manufacturer> field service"). A department-scoped user's unit is `department`, matched to a Department by name.
    company = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_invited = models.BooleanField(default=False, help_text="Invitation sent, first sign-in pending.")
    invited_at = models.DateTimeField(null=True, blank=True, help_text="When the latest invitation email was sent; resending replaces the link.")

    def level_for(self, module: str) -> int:
        if self.is_superuser:
            return Level.FULL
        if not self.role_id:
            return Level.NONE
        return self.role.level_for(module)

    def has_level(self, module: str, level: int) -> bool:
        return self.level_for(module) >= level

    class Meta(AbstractUser.Meta):
        constraints = [
            # One account per person and facility. NULLs are distinct, so every account in one facility passes.
            models.UniqueConstraint(fields=["person", "tenant"], name="uniq_person_per_facility"),
            # A person's accounts are facility accounts: never a platform superuser's (no facility).
            models.CheckConstraint(condition=models.Q(person__isnull=True) | models.Q(tenant__isnull=False), name="person_needs_facility"),
        ]

    def __str__(self):
        # The email before the username: a second facility's account has a username of its own ("<email>@<facility slug>").
        return self.get_full_name() or self.email or self.username

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        # set_password() leaves the raw password in _password until the save; check_password()'s hasher upgrade clears it and
        # saves the password column alone. Either way the column changed.
        password_set = self._password is not None or (update_fields is not None and "password" in update_fields)
        if update_fields is None and not self._state.adding and not kwargs.get("force_insert") and not args:
            # Never `person` on a full save of a row that exists: it is set when the row is created and by the linking UPDATE
            # (services.add_account), and an account loaded before another facility linked it (a Change password or a reset
            # taking a second to hash) would otherwise write its stale empty value back and orphan the link.
            deferred = self.get_deferred_fields()
            kwargs["update_fields"] = [f.name for f in self._meta.concrete_fields
                                       if not f.primary_key and f.name != "person" and f.attname not in deferred]
        if password_set and not self.person and not self._state.adding:
            # the link may have been made since this row was loaded: share with the person it has now
            self.person = User._default_manager.filter(pk=self.pk).values_list("person", flat=True).first()
        if not (self.person and password_set and self.has_usable_password()):
            return super().save(*args, **kwargs)
        with transaction.atomic():
            super().save(*args, **kwargs)
            share_password(self)


def share_password(account) -> None:
    """Write `account`'s (usable) password to its person's other accounts that have a usable one, and give the person's pending
    invitations a fresh unusable password, so a set-password link already emailed stops working: the person has a password now
    and joins those facilities from the facility menu (apps.web.views_facilities). One UPDATE per kind, on the password column
    only: nothing else a concurrent change wrote (a deactivation, a role) is undone. User has no history table, so this touches
    no tenant-scoped row and runs on signed-out paths (a reset, an invitation) as well."""
    others = User._default_manager.filter(person=account.person).exclude(pk=account.pk)
    others.exclude(password__startswith=UNUSABLE_PASSWORD_PREFIX).update(password=account.password)
    for pk in others.filter(password__startswith=UNUSABLE_PASSWORD_PREFIX).values_list("pk", flat=True):
        User._default_manager.filter(pk=pk, password__startswith=UNUSABLE_PASSWORD_PREFIX).update(password=make_password(None))


DEFAULT_ROLES = [
    # slug, name, description, {module: level}
    ("director", "Director", "Everything, including policy, contracts, integrations, and user management", {m: Level.FULL for m in Module.values}),
    ("manager", "CE manager", "Assigns and closes work, approves AEM changes and recall closures",
     {"equipment": Level.EDIT, "workorders": Level.APPROVE, "pm": Level.APPROVE, "contracts": Level.EDIT, "recalls": Level.APPROVE,
      "reports": Level.VIEW, "users": Level.VIEW, "settings": Level.VIEW, "incidents": Level.APPROVE}),
    ("technician", "Technician", "Work orders, PM checklists, and parts requests; cannot close recalls",
     {"equipment": Level.EDIT, "workorders": Level.EDIT, "pm": Level.EDIT, "contracts": Level.VIEW, "recalls": Level.VIEW,
      "reports": Level.VIEW, "users": Level.NONE, "settings": Level.NONE, "incidents": Level.EDIT}),
    ("requester", "Clinical requester", "Submits requests and sees status for their own unit",
     {"equipment": Level.VIEW, "workorders": Level.REQUEST, "pm": Level.NONE, "contracts": Level.NONE, "recalls": Level.NONE,
      "reports": Level.NONE, "users": Level.NONE, "settings": Level.NONE, "incidents": Level.NONE}),
    ("analyst", "Finance and quality", "Dashboards and reports, read-only",
     {"equipment": Level.VIEW, "workorders": Level.VIEW, "pm": Level.VIEW, "contracts": Level.VIEW, "recalls": Level.VIEW,
      "reports": Level.FULL, "users": Level.NONE, "settings": Level.NONE, "incidents": Level.VIEW}),
    ("vendor", "Vendor technician", "Sees and updates only work orders assigned to their company",
     {"equipment": Level.VIEW, "workorders": Level.EDIT, "pm": Level.NONE, "contracts": Level.NONE, "recalls": Level.NONE,
      "reports": Level.NONE, "users": Level.NONE, "settings": Level.NONE, "incidents": Level.NONE}),
]
# A default role's level for a module it has no row for (Role.levels): a facility made before a module existed gets its default.
DEFAULT_LEVELS = {slug: levels for slug, _name, _desc, levels in DEFAULT_ROLES}


def create_default_roles(tenant):
    """Create the six standard roles for a new tenant. Idempotent. Runs inside the tenant, so the rows pass row-level
    security whoever calls it (bootstrap_tenant and seed_demo run with no tenant set)."""
    created = []
    with tenant_context(tenant):
        for slug, name, desc, levels in DEFAULT_ROLES:
            # unscoped: the tenant is explicit here; the DB setting above is what row-level security checks
            role, was_created = Role.unscoped.get_or_create(tenant=tenant, slug=slug,
                                                            defaults={"name": name, "description": desc, "is_system": slug == "director"})
            role.set_levels(levels)
            if was_created:
                created.append(role)
    return created


class AccessEvent(TenantModel):
    """One change to who can do what in a facility (slice 20): an invitation, a user's role, unit, or company, deactivating and
    reactivating an account, and a role's levels and scope. apps.accounts.services writes one row per change, beside its log line,
    and the Users and access tab's change log (apps.core.history) reads them with the records' own histories. Never edited."""

    class Action(models.TextChoices):
        INVITED = "invited", "Invited"
        INVITATION_RESENT = "invitation_resent", "Invitation resent"
        ROLE_CHANGED = "role_changed", "Role changed"
        SCOPE_CHANGED = "scope_changed", "Company or unit changed"
        DEACTIVATED = "deactivated", "Deactivated"
        REACTIVATED = "reactivated", "Reactivated"
        ROLE_CREATED = "role_created", "Role added"
        ROLE_LEVEL_CHANGED = "role_level_changed", "Role access changed"
        ROLE_SCOPE_CHANGED = "role_scope_changed", "What a role sees changed"

    at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=30, choices=Action.choices)
    by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="Who made the change")
    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="Whose account changed")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="Which role changed, or was given")
    detail = models.CharField(max_length=300, blank=True, help_text="What changed, in words: 'Technician → CE manager', 'Contracts: View → Edit'")

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["tenant", "at"])]

    def __str__(self):
        return f"{self.get_action_display()}: {self.detail}"
