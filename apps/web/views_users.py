"""
Users and access: the Users tab and the Roles and permissions tab (slice 5). The Change log tab (slice 20) is
views_change_log.py, sharing tabs_context.

User is not tenant-scoped, so every lookup here filters on request.tenant; another tenant's user is a 404, never a leak.
Row actions return the refreshed row and raise 'users-changed' so the list (and its filters) catch up; the matrix is
returned whole. State changes go through apps.accounts.services; invitation emails (Invite user, Resend invite, slice 10)
through apps.accounts.invitations, whose send never raises on a mail failure, so the account change always stands.

Slice 16 (who sees what): a user's company or department is set in Invite user and in Edit on their row, and the list says
which one a scoped user sees by, or that they see nothing yet. Choosing a company- or department-scoped role in a row's role
select for someone without what it needs opens Edit with that role chosen, to ask for it. The matrix shows each role's scope
and changes a custom role's (the Director's and the default vendor and requester roles' are fixed).
"""
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import reswap, retarget, trigger_client_event

from apps.accounts import invitations, services
from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.accounts.services import USER_STATUSES
from apps.credentials.models import Credential, Technician
from apps.workorders.scoping import scope_of

from .decorators import web_view
from .forms import parse_uuid
from .forms_users import COMPANY_LIST_ID, InviteUserForm, NewRoleForm, UserAccessForm, parse_user_filters
from .htmx import is_partial, toast

# The matrix's short words for a role's scope (DataScope's labels are the long form, used in Add role).
SCOPE_SHORT = {DataScope.FACILITY: "Whole facility", DataScope.COMPANY: "Own company", DataScope.DEPARTMENT: "Own department"}


def tabs_context(request, tab: str) -> dict:
    """What web/_users_tabs.html needs, for each of the Users and access tabs."""
    return {
        "nav_active": "users", "users_tab": tab,
        "users_summary": {"active_users": services.count_active_users(request.tenant), "roles": Role.objects.count(),
                          "technicians": Technician.objects.filter(is_active=True).count()},
        "can_manage_users": request.user.has_level(Module.USERS, Level.FULL),
        "can_manage_credentials": request.user.has_level(Module.USERS, Level.EDIT),
    }


def _name(user) -> str:
    return user.get_full_name() or user.username


# --- Users tab ------------------------------------------------------------------------------------

def _rows(request, users) -> list[dict]:
    """One dict per user with what the row template needs; credential counts come from one query, not one per row."""
    horizon = date.today() + timedelta(days=settings.CREDENTIAL_EXPIRY_WARNING_DAYS)
    techs = {t.user_id: t for t in Technician.objects.filter(user__in=[u.pk for u in users]).prefetch_related("credentials")}
    departments = services.department_names(request.tenant)
    rows = []
    for u in users:
        tech = techs.get(u.pk)
        creds = [c for c in tech.credentials.all() if c.status == Credential.Status.ACTIVE] if tech else []
        rows.append({"u": u, "status": services.user_status(u), "tech": tech, "cred_count": len(creds),
                     "expiring": sum(1 for c in creds if c.expires_on and c.expires_on <= horizon), "is_self": u.pk == request.user.pk,
                     # Resend invite only where a link could still set the first password (not after a password was set in Admin)
                     "pending": invitations.is_pending(u),
                     # slice 16: what a scoped user sees by, and what is missing when they see nothing
                     "scope": scope_of(u), "gap": services.scope_gap(u, departments)})
    return rows


def _users_context(request) -> dict:
    roles = services.role_order(Role.objects.all())
    f = parse_user_filters(request.GET, {r.slug for r in roles})
    return {**tabs_context(request, "users"),"list_url": reverse("web:users"), "f": f, "roles": roles, "statuses": USER_STATUSES,
            "rows": _rows(request, services.list_users(request.tenant, f))}


@web_view(Module.USERS, Level.VIEW)
def users(request):
    ctx = _users_context(request)
    if is_partial(request, "users-body"):
        return render(request, "web/_users_body.html", {**ctx, "oob_summary": True})
    return render(request, "web/users.html", ctx)


def _get_user(request, pk):
    return get_object_or_404(User.objects.select_related("role", "tenant"), pk=pk, tenant=request.tenant)


def _body_response(request, message: str):
    """Row actions return the whole body with the current filters (the POST URL carries them) plus the summary line."""
    return toast(render(request, "web/_users_body.html", {**_users_context(request), "oob_summary": True}), message)


def _is_scope_error(error: ValidationError) -> bool:
    return hasattr(error, "error_dict") and bool({"company", "department"} & set(error.error_dict))


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_role(request, pk):
    user = _get_user(request, pk)
    role = Role.objects.filter(pk=parse_uuid(request.POST.get("role"))).first()
    try:
        services.set_user_role(user, role, by=request.user)
        return _body_response(request, f"{_name(user)}: role set to {role.name}")
    except ValidationError as e:
        if not _is_scope_error(e):
            return _body_response(request, e.messages[0])
        # The role needs a company or department the account does not have: ask for it in Edit, with the role chosen. The list
        # re-fetches meanwhile, so the row's select shows the role the user still has until Edit saves.
        form = UserAccessForm({"role": str(role.id), "company": user.company, "department": user.department}, user=user)
        form.is_valid()
        form.add_service_errors(e)
        form.focus_first_error()
        response = reswap(retarget(_edit_modal(request, user, form), "#modal-card"), "innerHTML")
        return trigger_client_event(response, "users-changed", {})


def _edit_modal(request, user, form):
    return render(request, "web/_user_edit.html", {"form": form, "target": user, "company_list_id": COMPANY_LIST_ID})


@web_view(Module.USERS, Level.FULL)
def user_edit(request, pk):
    """Edit on a row: role, company, and department. GET with the form's values (the role select re-renders it for the role chosen)."""
    user = _get_user(request, pk)
    is_self = user.pk == request.user.pk
    if request.method != "POST":
        params = request.GET.dict()
        return _edit_modal(request, user, UserAccessForm(user=user, is_self=is_self, initial=params, focus="role" if params else None))
    form = UserAccessForm(request.POST, user=user, is_self=is_self)
    if form.is_valid():
        try:
            services.set_user_access(user, role=form.cleaned_data["role"], **form.scope_values(), by=request.user)
            return _modal_done(f"{_name(user)} updated", "users-changed")
        except ValidationError as e:
            form.add_service_errors(e)
            form.focus_first_error()
    return _edit_modal(request, user, form)


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_deactivate(request, pk):
    user = _get_user(request, pk)
    try:
        services.deactivate_user(user, by=request.user)
        return _body_response(request, f"{_name(user)} deactivated")
    except ValidationError as e:
        return _body_response(request, e.messages[0])


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_reactivate(request, pk):
    user = _get_user(request, pk)
    try:
        services.reactivate_user(user, by=request.user)
        return _body_response(request, f"{_name(user)} reactivated")
    except ValidationError as e:
        return _body_response(request, e.messages[0])


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_resend_invite(request, pk):
    """A fresh invitation link; the earlier one stops working. Only for accounts still waiting to set their first password."""
    user = _get_user(request, pk)
    try:
        sent = invitations.send_invitation(user, by=request.user)
    except ValidationError as e:
        return _body_response(request, e.messages[0])
    if sent:
        return _body_response(request, f"Invitation resent to {user.email}")
    return _body_response(request, f"The invitation email to {user.email} could not be sent. Try again once email is working.")


def _modal_done(message: str, event: str):
    """Empty the modal card, tell the list to refresh, then close. Closing first would detach the form and cancel the swap."""
    response = toast(HttpResponse(""), message)
    trigger_client_event(response, event, {})
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _invite_modal(request, form):
    return render(request, "web/_user_invite.html", {"form": form, "valid_days": settings.INVITATION_VALID_DAYS, "company_list_id": COMPANY_LIST_ID})


@web_view(Module.USERS, Level.FULL)
def user_invite(request):
    if request.method != "POST":
        # with the form's values: the role select re-renders the form for the role chosen (its company and department fields)
        params = request.GET.dict()
        return _invite_modal(request, InviteUserForm(initial=params, focus="role") if params else InviteUserForm())
    form = InviteUserForm(request.POST)
    if not form.is_valid():
        form.focus_first_error()
        return _invite_modal(request, form)
    d = form.cleaned_data
    try:
        user = services.invite_user(request.tenant, email=d["email"], first_name=d["first_name"], last_name=d["last_name"], role=d["role"],
                                    create_technician=d["create_technician"], by=request.user, **form.scope_values())
    except ValidationError as e:
        form.add_service_errors(e)
        form.focus_first_error()
        return _invite_modal(request, form)
    # The account exists whatever happens to the email. A failed send keeps the modal open with the warning (a toast fades
    # before it is read); Resend invite tries again.
    if invitations.send_invitation(user, by=request.user):
        return _modal_done(f"Invitation sent to {user.email}", "users-changed")
    return trigger_client_event(render(request, "web/_user_invite_unsent.html", {"email": user.email}), "users-changed", {})


# --- Roles tab ------------------------------------------------------------------------------------

# Column order from the mock's MODULES list (Recalls before Contracts), not the enum's declaration order.
ROLE_MATRIX_MODULES = [Module.EQUIPMENT, Module.WORKORDERS, Module.PM, Module.RECALLS, Module.CONTRACTS, Module.REPORTS, Module.USERS, Module.SETTINGS]

def _roles_context(request) -> dict:
    counts = services.role_user_counts(request.tenant)
    matrix = []
    for role in services.role_order(Role.objects.prefetch_related("permissions")):
        levels = {p.module: p.level for p in role.permissions.all()}
        matrix.append({"role": role, "cells": [(m.value, levels.get(m.value, Level.NONE)) for m in ROLE_MATRIX_MODULES], "users": counts.get(role.id, 0),
                       "scope": role.effective_scope, "scope_fixed": services.scope_fixed_reason(role)})
    return {**tabs_context(request, "roles"),"matrix": matrix, "modules": [(m.value, m.label) for m in ROLE_MATRIX_MODULES], "levels": Level.choices,
            "scopes": [(v, SCOPE_SHORT[v], label) for v, label in DataScope.choices],
            "oob_summary": request.htmx is not None and bool(request.htmx)}


@web_view(Module.USERS, Level.VIEW)
def roles(request):
    ctx = _roles_context(request)
    if is_partial(request, "roles-matrix"):
        return render(request, "web/_roles_matrix.html", ctx)
    return render(request, "web/roles.html", ctx)


@require_POST
@web_view(Module.USERS, Level.FULL)
def role_level(request, pk):
    role = get_object_or_404(Role.objects, pk=pk)
    module = request.POST.get("module", "")
    try:
        level = int(request.POST.get("level", ""))
    except ValueError:
        level = -1
    try:
        services.set_role_level(role, module, level, by=request.user)
        message = f"{role.name}: {Module(module).label} set to {Level(level).label}"
    except ValidationError as e:
        message = e.messages[0]
    return toast(render(request, "web/_roles_matrix.html", _roles_context(request)), message)


def _scope_message(role) -> str:
    message = f"{role.name}: sees {DataScope(role.effective_scope).label.lower()}"
    missing = services.users_without_scope(role)
    if missing:
        need = "company" if role.effective_scope == DataScope.COMPANY else "department"
        n = len(missing)
        message += f". {n} of its users {'has' if n == 1 else 'have'} no {need} and {'sees' if n == 1 else 'see'} nothing until one is set (Users tab)"
    return message


@require_POST
@web_view(Module.USERS, Level.FULL)
def role_scope(request, pk):
    role = get_object_or_404(Role.objects, pk=pk)
    try:
        services.set_role_scope(role, request.POST.get("scope", ""), by=request.user)
        message = _scope_message(role)
    except ValidationError as e:
        message = e.messages[0]
    return toast(render(request, "web/_roles_matrix.html", _roles_context(request)), message)


@web_view(Module.USERS, Level.FULL)
def role_new(request):
    if request.method != "POST":
        return render(request, "web/_role_new.html", {"form": NewRoleForm()})
    form = NewRoleForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            role = services.create_role(name=d["name"], description=d["description"], copy_from=d["copy_from"], scope=d["scope"], by=request.user)
            return _modal_done(f'Role "{role.name}" created', "roles-changed")
        except ValidationError as e:
            form.add_error(None, e.messages[0])
    return render(request, "web/_role_new.html", {"form": form})
