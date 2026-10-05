"""
Serializers for users and access over the API (slice 19, part D; the endpoints are in apps/api/views_users.py).

They parse and show; none of them saves a row. Every write goes through apps.accounts.services (invite, role and scope changes,
deactivation, roles), apps.accounts.invitations (the emails), and apps.credentials.services (credentials), as the Users and access
screen's do. What a user shows never includes a password hash, an API token, or an invitation link.
"""
import uuid

from rest_framework import serializers

from apps.accounts import invitations, services
from apps.accounts.models import DEFAULT_ROLES, DataScope, Level, Module, Role
from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import credential_options, credential_state
from apps.tenants.context import get_current_tenant
from apps.web.forms_credentials import SOURCES
from apps.workorders.scoping import scope_of

from . import serializers as s

STANDARD_ROLE_SLUGS = frozenset(slug for slug, *_rest in DEFAULT_ROLES)


# --- fields --------------------------------------------------------------------------------------------------------------------

class _TenantRowField(serializers.Field):
    """One of this facility's rows by id, read through the tenant-scoped manager inside the request (never at import time). A
    malformed id, another facility's, or a row the field does not offer all read as the same refusal."""

    default_error_messages = {"invalid": "Choose one from this facility."}

    def rows(self):
        raise NotImplementedError

    def to_internal_value(self, data):
        try:
            pk = uuid.UUID(str(data))
        except (TypeError, ValueError, AttributeError):
            self.fail("invalid")
        row = self.rows().filter(pk=pk).first()
        if row is None:
            self.fail("invalid")
        return row

    def to_representation(self, value):
        return str(value.pk)


class RoleField(_TenantRowField):
    default_error_messages = {"invalid": "Choose a role.", "null": "Choose a role."}  # the forms' words

    def rows(self):
        return Role.objects.all()


class ActiveTechnicianField(_TenantRowField):
    """As the Add credential modal offers them: the facility's active technicians."""

    default_error_messages = {"invalid": "Choose one of this facility's active technicians."}

    def rows(self):
        return Technician.objects.filter(is_active=True)


# --- users ---------------------------------------------------------------------------------------------------------------------

class UserSerializer(serializers.Serializer):
    """A row of the Users tab. `role` is the role's id (what PATCH takes). `scope` is what the user sees by (their role's data
    scope); `company` shows only where the role sees by company (null otherwise), and `scope_gap` says what a scoped user is
    missing ("company" or "department") while they see nothing. `invitation_pending` is whether Resend invite applies."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.SerializerMethodField()
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    email = serializers.CharField(read_only=True)
    username = serializers.CharField(read_only=True)
    role = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    role_slug = serializers.SerializerMethodField()
    scope = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    department = serializers.CharField(read_only=True)
    scope_gap = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    invitation_pending = serializers.SerializerMethodField()
    last_sign_in = serializers.DateTimeField(source="last_login", read_only=True)
    is_superuser = serializers.BooleanField(read_only=True)
    technician = serializers.SerializerMethodField()

    def _departments(self):
        if "departments" not in self.context:  # one query for a whole page, not one per row
            self.context["departments"] = services.department_names(get_current_tenant())
        return self.context["departments"]

    def get_name(self, u):
        return u.get_full_name() or u.username

    def get_role(self, u):
        return str(u.role_id) if u.role_id else None

    def get_role_name(self, u):
        return u.role.name if u.role_id else None

    def get_role_slug(self, u):
        return u.role.slug if u.role_id else None

    def get_scope(self, u):
        return scope_of(u)

    def get_company(self, u):
        return u.company if scope_of(u) == DataScope.COMPANY else None

    def get_scope_gap(self, u):
        return services.scope_gap(u, self._departments()) or None

    def get_status(self, u):
        return services.user_status(u)

    def get_invitation_pending(self, u):
        return invitations.is_pending(u)

    def get_technician(self, u):
        techs = self.context.get("technicians")  # {user id: technician id} for the rows the view serializes (one query)
        tech = techs.get(u.pk) if techs is not None else Technician.objects.filter(user=u).values_list("id", flat=True).first()
        return str(tech) if tech else None


class InviteSerializer(serializers.Serializer):
    """Invite user's fields. The company is for a role that sees by company; the department is one of the facility's for a
    department-scoped role, and otherwise one of the names the modal offers (the view checks both against the role chosen)."""

    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    # The address becomes the username (150 characters at most): a longer one is refused here rather than failing to save.
    email = serializers.EmailField(max_length=150)
    role = RoleField()
    company = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    department = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)
    create_technician = serializers.BooleanField(required=False, default=False)


class UserAccessSerializer(serializers.Serializer):
    """Edit on a user's row: the role, the company, and the department, each optional (one left out keeps the account's)."""

    role = RoleField(required=False)
    company = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    department = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)


# --- roles ---------------------------------------------------------------------------------------------------------------------

class RoleSerializer(serializers.Serializer):
    """A row of the Roles and permissions matrix. `levels` is every module's level (0 None, 1 View, 2 Request, 3 Edit, 4 Approve,
    5 Full); `is_system` marks the Director, whose levels and scope never change; `standard` the six roles every facility starts
    with. `scope` is who the role's users see, `scope_fixed` why it cannot change (null when it can), `users` its active users
    (invited ones included), and `users_seeing_nothing` those of them who see nothing until their company or department is set."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    is_system = serializers.BooleanField(read_only=True)
    standard = serializers.SerializerMethodField()
    levels = serializers.SerializerMethodField()
    scope = serializers.SerializerMethodField()
    scope_fixed = serializers.SerializerMethodField()
    users = serializers.SerializerMethodField()
    users_seeing_nothing = serializers.SerializerMethodField()

    def get_standard(self, role):
        return role.slug in STANDARD_ROLE_SLUGS

    def get_levels(self, role):
        held = {p.module: p.level for p in role.permissions.all()}
        return {m: held.get(m, Level.NONE) for m in Module.values}

    def get_scope(self, role):
        return role.effective_scope

    def get_scope_fixed(self, role):
        return services.scope_fixed_reason(role) or None

    def get_users(self, role):
        if "role_counts" not in self.context:
            self.context["role_counts"] = services.role_user_counts(get_current_tenant())
        return self.context["role_counts"].get(role.id, 0)

    def get_users_seeing_nothing(self, role):
        return 0 if role.effective_scope == DataScope.FACILITY else len(services.users_without_scope(role))


class NewRoleSerializer(serializers.Serializer):
    """Add role: a name, the role whose levels it starts with, a description, and who it sees (the whole facility if not given)."""

    name = serializers.CharField(max_length=80)
    copy_from = RoleField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")
    scope = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_scope(self, value):
        return value or DataScope.FACILITY  # not sent: the whole facility, as the modal's select starts


class RoleChangeSerializer(serializers.Serializer):
    """A role's matrix row: {module: level} for the cells to change, and who the role sees."""

    levels = serializers.DictField(child=serializers.IntegerField(), required=False)
    scope = serializers.CharField(required=False)


# --- technicians and credentials -----------------------------------------------------------------------------------------------

class CredentialSerializer(s.CredentialSerializer):
    """A credential as the tab shows it; `state` is its chip: in_training, expired, expiring, or ok."""

    technician_name = serializers.CharField(source="technician.name", read_only=True)
    state = serializers.SerializerMethodField()

    class Meta(s.CredentialSerializer.Meta):
        fields = [*s.CredentialSerializer.Meta.fields, "technician_name", "state"]
        read_only_fields = fields

    def get_state(self, obj):
        return credential_state(obj)["key"]


class TechnicianSerializer(s.TechnicianSerializer):
    class Meta(s.TechnicianSerializer.Meta):
        read_only_fields = s.TechnicianSerializer.Meta.fields


def _catalog_covers() -> set[tuple[str, str]]:
    """The (scope, value) pairs Add credential's "Covers" offers: the catalog's categories, manufacturers, and models."""
    return {tuple(value.split("|", 1)) for _group, options in credential_options() for value, _label in options}


class NewCredentialSerializer(serializers.Serializer):
    """Add credential's fields: what it covers is one of the catalog's categories, manufacturers, or models (scope and value), and
    its source one of the modal's four."""

    technician = ActiveTechnicianField()
    scope = serializers.ChoiceField(choices=Scope.choices)
    value = serializers.CharField(max_length=120)
    source = serializers.ChoiceField(choices=[(x, x) for x in SOURCES])
    status = serializers.ChoiceField(choices=Credential.Status.choices, required=False, default=Credential.Status.ACTIVE)
    issued_on = serializers.DateField()
    expires_on = serializers.DateField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        if (attrs["scope"], attrs["value"]) not in _catalog_covers():
            raise serializers.ValidationError({"value": ["Choose what the credential covers: a category, manufacturer, or model in this "
                                                         "facility's catalog."]})
        if attrs.get("expires_on") and attrs["expires_on"] < attrs["issued_on"]:
            raise serializers.ValidationError({"expires_on": ["The expiry date cannot be before the issue date."]})
        return attrs
