from django.contrib.auth.models import AbstractUser
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class Module(models.TextChoices):
    EQUIPMENT = "equipment", "Equipment"
    WORKORDERS = "workorders", "Work orders"
    PM = "pm", "PM schedule"
    CONTRACTS = "contracts", "Contracts"
    RECALLS = "recalls", "Recalls"
    REPORTS = "reports", "Reports"
    USERS = "users", "Users and access"
    SETTINGS = "settings", "Settings"


class Level(models.IntegerChoices):
    NONE = 0, "None"
    VIEW = 1, "View"
    REQUEST = 2, "Request"
    EDIT = 3, "Edit"
    APPROVE = 4, "Approve"
    FULL = 5, "Full"


class Role(TenantModel):
    """A named access level. Permissions are one row per module (see RolePermission)."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=40)
    description = models.CharField(max_length=300, blank=True)
    is_system = models.BooleanField(default=False, help_text="System roles (e.g. Director) cannot be edited or deleted.")
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "slug"], name="uniq_role_slug_per_tenant")]

    def __str__(self):
        return self.name

    def level_for(self, module: str) -> int:
        perm = self.permissions.filter(module=module).first()
        return perm.level if perm else Level.NONE

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
    """Users belong to exactly one tenant. Superusers may have no tenant and switch in the admin."""

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, null=True, blank=True, related_name="users")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")
    department = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_invited = models.BooleanField(default=False, help_text="Invitation sent, first sign-in pending.")

    def level_for(self, module: str) -> int:
        if self.is_superuser:
            return Level.FULL
        if not self.role_id:
            return Level.NONE
        return self.role.level_for(module)

    def has_level(self, module: str, level: int) -> bool:
        return self.level_for(module) >= level

    def __str__(self):
        return self.get_full_name() or self.username


DEFAULT_ROLES = [
    # slug, name, description, {module: level}
    ("director", "Director", "Everything, including policy, contracts, integrations, and user management", {m: Level.FULL for m in Module.values}),
    ("manager", "CE manager", "Assigns and closes work, approves AEM changes and recall closures",
     {"equipment": Level.EDIT, "workorders": Level.APPROVE, "pm": Level.APPROVE, "contracts": Level.EDIT, "recalls": Level.APPROVE,
      "reports": Level.VIEW, "users": Level.VIEW, "settings": Level.VIEW}),
    ("technician", "Technician", "Work orders, PM checklists, and parts requests; cannot close recalls",
     {"equipment": Level.EDIT, "workorders": Level.EDIT, "pm": Level.EDIT, "contracts": Level.VIEW, "recalls": Level.VIEW,
      "reports": Level.VIEW, "users": Level.NONE, "settings": Level.NONE}),
    ("requester", "Clinical requester", "Submits requests and sees status for their own unit",
     {"equipment": Level.VIEW, "workorders": Level.REQUEST, "pm": Level.NONE, "contracts": Level.NONE, "recalls": Level.NONE,
      "reports": Level.NONE, "users": Level.NONE, "settings": Level.NONE}),
    ("analyst", "Finance and quality", "Dashboards and reports, read-only",
     {"equipment": Level.VIEW, "workorders": Level.VIEW, "pm": Level.VIEW, "contracts": Level.VIEW, "recalls": Level.VIEW,
      "reports": Level.FULL, "users": Level.NONE, "settings": Level.NONE}),
    ("vendor", "Vendor technician", "Sees and updates only work orders assigned to their company",
     {"equipment": Level.VIEW, "workorders": Level.EDIT, "pm": Level.NONE, "contracts": Level.NONE, "recalls": Level.NONE,
      "reports": Level.NONE, "users": Level.NONE, "settings": Level.NONE}),
]


def create_default_roles(tenant):
    """Create the six standard roles for a new tenant. Idempotent."""
    created = []
    for slug, name, desc, levels in DEFAULT_ROLES:
        role, was_created = Role.unscoped.get_or_create(tenant=tenant, slug=slug, defaults={"name": name, "description": desc, "is_system": slug == "director"})
        role.set_levels(levels)
        if was_created:
            created.append(role)
    return created
