"""
Input parsing for the Users and access screen. Role choices depend on the tenant, so they are built in __init__;
query-string filters are validated against the allowed values and anything unknown is dropped.
"""
from django import forms

from apps.accounts.models import Role
from apps.accounts.services import USER_STATUSES, UserFilters
from apps.equipment.models import Department

DEFAULT_ROLE_SLUG = "requester"
# Departments the invite form suggests besides the tenant's clinical departments (the mock's list).
STANDING_DEPARTMENTS = (["Clinical Engineering"], ["Finance", "Quality and Patient Safety", "External vendor"])


def _one_of(value, allowed) -> str:
    return value if value in allowed else ""


def parse_user_filters(params, role_slugs: set[str]) -> UserFilters:
    return UserFilters(q=params.get("q", "").strip()[:100], role=_one_of(params.get("role"), role_slugs),
                       status=_one_of(params.get("status"), {k for k, _ in USER_STATUSES}))


def department_options() -> list[str]:
    before, after = STANDING_DEPARTMENTS
    names = before + list(Department.objects.values_list("name", flat=True)) + after
    return list(dict.fromkeys(names))


class RoleChoiceMixin:
    """A role select over the tenant's roles; cleaned to the Role instance."""

    field = "role"

    def _init_roles(self):
        self.roles = {str(r.id): r for r in Role.objects.all()}
        default = next((rid for rid, r in self.roles.items() if r.slug == DEFAULT_ROLE_SLUG), next(iter(self.roles), ""))
        self.fields[self.field].choices = [(rid, r.name) for rid, r in self.roles.items()]
        self.fields[self.field].initial = default
        self.fields[self.field].error_messages["invalid_choice"] = "Choose a role."

    def _clean_role(self, value):
        role = self.roles.get(value)
        if role is None:
            raise forms.ValidationError("Choose a role.")
        return role


class InviteUserForm(RoleChoiceMixin, forms.Form):
    first_name = forms.CharField(max_length=150, label="First name", widget=forms.TextInput(attrs={"autofocus": True}))
    last_name = forms.CharField(max_length=150, label="Last name")
    email = forms.EmailField(label="Work email", widget=forms.EmailInput(attrs={"placeholder": "name@hospital.org"}))
    role = forms.ChoiceField(label="Role")
    department = forms.ChoiceField(label="Department", required=False)
    create_technician = forms.BooleanField(required=False, label="Also create a technician profile so credentials and work can be assigned")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_roles()
        # The mock's select: Clinical Engineering first, then the tenant's departments, then the standing non-clinical ones.
        self.fields["department"].choices = [(d, d) for d in department_options()]
        self.fields["department"].initial = STANDING_DEPARTMENTS[0][0]

    def clean_role(self):
        return self._clean_role(self.cleaned_data["role"])

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class NewRoleForm(RoleChoiceMixin, forms.Form):
    field = "copy_from"

    name = forms.CharField(max_length=80, label="Role name", widget=forms.TextInput(attrs={"placeholder": "e.g. Sterile processing lead", "autofocus": True}))
    copy_from = forms.ChoiceField(label="Copy permissions from")
    description = forms.CharField(max_length=300, required=False, widget=forms.TextInput(attrs={"placeholder": "What this role is for"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_roles()

    def clean_copy_from(self):
        return self._clean_role(self.cleaned_data["copy_from"])
