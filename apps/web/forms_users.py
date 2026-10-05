"""
Input parsing for the Users and access screen. Role choices depend on the tenant, so they are built in __init__;
query-string filters are validated against the allowed values and anything unknown is dropped.

Slice 16: Invite user and Edit lay out the company and department fields for the chosen role's scope (choosing another role
re-renders the form): a company-scoped role asks for the company (the facility's vendor names suggested, any name allowed), and a
department-scoped role offers only the facility's departments. The rules themselves are accounts.services' (_scoped_values), whose
errors land on the fields they name.
"""
from django import forms
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import DataScope, Role
from apps.accounts.services import USER_STATUSES, UserFilters
from apps.tenants.context import get_current_tenant

from .forms_models import ServiceErrorsMixin

DEFAULT_ROLE_SLUG = "requester"
VENDOR_DEPARTMENT = "External vendor"  # where a company-scoped user's department starts in Invite user
CHOOSE_UNIT = "Choose their unit"
COMPANY_LIST_ID = "user-companies"  # the datalist of suggested companies (one modal is open at a time)
SCOPE_HINTS = {
    DataScope.COMPANY: "This role sees only the work orders assigned to their company (under its name, or its name followed by "
                       "“field service”) and the devices those work orders are on.",
    DataScope.DEPARTMENT: "This role sees only the devices in their unit and those devices' work orders.",
}


def _one_of(value, allowed) -> str:
    return value if value in allowed else ""


def parse_user_filters(params, role_slugs: set[str]) -> UserFilters:
    return UserFilters(q=params.get("q", "").strip()[:100], role=_one_of(params.get("role"), role_slugs),
                       status=_one_of(params.get("status"), {k for k, _ in USER_STATUSES}))


# The Invite and Edit forms' department list lives in the service, shared with the API (apps/api/views_users.py).
department_options, facility_departments, STANDING_DEPARTMENTS = services.department_options, services.facility_departments, services.STANDING_DEPARTMENTS


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


def _company_field():
    return forms.CharField(max_length=services.COMPANY_MAX_LENGTH, required=False, label="Company",
                           widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "The vendor they work for"}))


def _department_field():
    # A select whose options follow the role's scope (_init_scope). A CharField, not a ChoiceField: for a department-scoped role
    # the service says why another name will not do, rather than "Select a valid choice".
    return forms.CharField(max_length=100, required=False, label="Department", widget=forms.Select)


class ScopeFieldsMixin(RoleChoiceMixin, ServiceErrorsMixin):
    """The company and department fields, laid out for the scope of the role the form has chosen (self.chosen_role)."""

    layout = ("role", "company", "department")

    def _chosen_role(self):
        role_field = self.fields["role"]
        value = self.data.get("role") if self.is_bound and not role_field.disabled else self.initial.get("role", role_field.initial)
        return self.roles.get(str(value or ""))

    def _init_scope(self, *, rerender_url: str, form_id: str, focus: str | None = None, vendor_department: str = ""):
        # choosing a role GETs the form again with its values, laid out for that role
        self.fields["role"].widget.attrs.update({"hx-get": rerender_url, "hx-include": f"#{form_id}", "hx-target": "#modal-card", "hx-trigger": "change"})
        self.chosen_role = self._chosen_role()
        self.scope = self.chosen_role.effective_scope if self.chosen_role else DataScope.FACILITY
        self.scope_hint = SCOPE_HINTS.get(self.scope, "")
        self.shows_company = self.scope == DataScope.COMPANY
        self.company_suggestions = services.company_suggestions(get_current_tenant()) if self.shows_company else []
        if self.shows_company:
            self.fields["company"].widget.attrs.update({"list": COMPANY_LIST_ID, "required": True})
        current = str(self.initial.get("department") or "").strip()
        if self.scope == DataScope.DEPARTMENT:
            units = facility_departments()
            self.department_choices = [("", CHOOSE_UNIT)] + [(n, n) for n in units]
            # the account's unit as the facility names it (it matches in any letter case); any other name waits for a choice
            self.initial["department"] = next((n for n in units if n.upper() == current.upper()), "")
        else:
            names = department_options()
            if current and current not in names:
                names.append(current)  # a department typed before there was a list stays as it is
            self.department_choices = [(n, n) for n in names]
            self.initial["department"] = current or (vendor_department if self.shows_company else "") or STANDING_DEPARTMENTS[0][0]
        self.fields["department"].widget.choices = self.department_choices
        if focus in self.fields:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[focus].widget.attrs["autofocus"] = True

    def clean_role(self):
        return self._clean_role(self.cleaned_data["role"])

    def clean_department(self):
        value = (self.cleaned_data.get("department") or "").strip()
        if self.scope != DataScope.DEPARTMENT and value and value not in {v for v, _ in self.department_choices}:
            raise forms.ValidationError("Choose a department from the list.")
        return value

    def scope_values(self) -> dict:
        """The company and department to save: the company only where the form showed it (None keeps the account's)."""
        d = self.cleaned_data
        return {"company": d.get("company", "") if self.shows_company else None, "department": d.get("department", "")}


class InviteUserForm(ScopeFieldsMixin, forms.Form):
    layout = ("first_name", "last_name", "email", "role", "company", "department")

    first_name = forms.CharField(max_length=150, label="First name", widget=forms.TextInput(attrs={"autofocus": True}))
    last_name = forms.CharField(max_length=150, label="Last name")
    email = forms.EmailField(label="Work email", widget=forms.EmailInput(attrs={"placeholder": "name@hospital.org"}))
    role = forms.ChoiceField(label="Role")
    company = _company_field()
    department = _department_field()
    create_technician = forms.BooleanField(required=False, label="Also create a technician profile so credentials and work can be assigned")

    def __init__(self, *args, focus: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_roles()
        # The mock's select: Clinical Engineering first, then the tenant's departments, then the standing non-clinical ones (External
        # vendor first for a company-scoped role); a department-scoped role, such as the clinical requester, picks one of the facility's.
        self._init_scope(rerender_url=reverse("web:user_invite"), form_id="nu-form", focus=focus, vendor_department=VENDOR_DEPARTMENT)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class UserAccessForm(ScopeFieldsMixin, forms.Form):
    """Edit on a user's row: role, company, and department in one change (accounts.services.set_user_access). Your own role is
    shown but never sent: you cannot change it."""

    role = forms.ChoiceField(label="Role")
    company = _company_field()
    department = _department_field()

    def __init__(self, *args, user, is_self: bool = False, focus: str | None = None, initial=None, **kwargs):
        start = {"company": user.company, "department": user.department, **({"role": str(user.role_id)} if user.role_id else {})}
        super().__init__(*args, initial={**start, **(initial or {})}, **kwargs)
        self.user = user
        self._init_roles()
        self.fields["role"].disabled = is_self
        self._init_scope(rerender_url=reverse("web:user_edit", args=[user.pk]), form_id="ue-form", focus=focus)


class NewRoleForm(RoleChoiceMixin, forms.Form):
    field = "copy_from"

    name = forms.CharField(max_length=80, label="Role name", widget=forms.TextInput(attrs={"placeholder": "e.g. Sterile processing lead", "autofocus": True}))
    copy_from = forms.ChoiceField(label="Copy permissions from")
    description = forms.CharField(max_length=300, required=False, widget=forms.TextInput(attrs={"placeholder": "What this role is for"}))
    scope = forms.ChoiceField(label="Sees", choices=DataScope.choices, initial=DataScope.FACILITY, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_roles()
        self.fields["scope"].error_messages["invalid_choice"] = services.SCOPE_CHOICE_MESSAGE

    def clean_copy_from(self):
        return self._clean_role(self.cleaned_data["copy_from"])

    def clean_scope(self):
        return self.cleaned_data["scope"] or DataScope.FACILITY  # not sent: the whole facility, as the select starts
