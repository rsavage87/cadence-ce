"""
Users and access: the Users tab and the Roles and permissions tab (slice 5).

User is not tenant-scoped, so every lookup here filters on request.tenant; another tenant's user is a 404, never a leak.
Row actions return the refreshed row and raise 'users-changed' so the list (and its filters) catch up; the matrix is
returned whole. State changes go through apps.accounts.services.
"""
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import trigger_client_event

from apps.accounts import services
from apps.accounts.models import Level, Module, Role, User
from apps.accounts.services import USER_STATUSES
from apps.credentials.models import Credential, Technician

from .decorators import web_view
from .forms import parse_uuid
from .forms_users import InviteUserForm, NewRoleForm, parse_user_filters
from .htmx import is_partial, toast


def _tabs_context(request, tab: str) -> dict:
    return {
        "nav_active": "users", "users_tab": tab,
        "users_summary": {"active_users": services.count_active_users(request.tenant), "roles": Role.objects.count(),
                          "technicians": Technician.objects.filter(is_active=True).count()},
        "can_manage_users": request.user.has_level(Module.USERS, Level.FULL),
        "can_manage_credentials": request.user.has_level(Module.USERS, Level.EDIT),
    }


# --- Users tab ------------------------------------------------------------------------------------

def _rows(request, users) -> list[dict]:
    """One dict per user with what the row template needs; credential counts come from one query, not one per row."""
    horizon = date.today() + timedelta(days=settings.CREDENTIAL_EXPIRY_WARNING_DAYS)
    techs = {t.user_id: t for t in Technician.objects.filter(user__in=[u.pk for u in users]).prefetch_related("credentials")}
    rows = []
    for u in users:
        tech = techs.get(u.pk)
        creds = [c for c in tech.credentials.all() if c.status == Credential.Status.ACTIVE] if tech else []
        rows.append({"u": u, "status": services.user_status(u), "tech": tech, "cred_count": len(creds),
                     "expiring": sum(1 for c in creds if c.expires_on and c.expires_on <= horizon), "is_self": u.pk == request.user.pk})
    return rows


def _users_context(request) -> dict:
    roles = services.role_order(Role.objects.all())
    f = parse_user_filters(request.GET, {r.slug for r in roles})
    return {**_tabs_context(request, "users"), "list_url": reverse("web:users"), "f": f, "roles": roles, "statuses": USER_STATUSES,
            "rows": _rows(request, services.list_users(request.tenant, f))}


@web_view(Module.USERS, Level.VIEW)
def users(request):
    ctx = _users_context(request)
    if is_partial(request, "users-body"):
        return render(request, "web/_users_body.html", {**ctx, "oob_summary": True})
    return render(request, "web/users.html", ctx)


def _get_user(request, pk):
    return get_object_or_404(User.objects.select_related("role"), pk=pk, tenant=request.tenant)


def _body_response(request, message: str):
    """Row actions return the whole body with the current filters (the POST URL carries them) plus the summary line."""
    return toast(render(request, "web/_users_body.html", {**_users_context(request), "oob_summary": True}), message)


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_role(request, pk):
    user = _get_user(request, pk)
    role = Role.objects.filter(pk=parse_uuid(request.POST.get("role"))).first()
    try:
        services.set_user_role(user, role, by=request.user)
        return _body_response(request, f"{user.get_full_name() or user.username}: role set to {role.name}")
    except ValidationError as e:
        return _body_response(request, e.messages[0])


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_deactivate(request, pk):
    user = _get_user(request, pk)
    try:
        services.deactivate_user(user, by=request.user)
        return _body_response(request, f"{user.get_full_name() or user.username} deactivated")
    except ValidationError as e:
        return _body_response(request, e.messages[0])


@require_POST
@web_view(Module.USERS, Level.FULL)
def user_reactivate(request, pk):
    user = _get_user(request, pk)
    try:
        services.reactivate_user(user, by=request.user)
        return _body_response(request, f"{user.get_full_name() or user.username} reactivated")
    except ValidationError as e:
        return _body_response(request, e.messages[0])



@require_POST
@web_view(Module.USERS, Level.FULL)
def user_resend_invite(request, pk):
    raise NotImplementedError  # scaffold: agent A


def _modal_done(message: str, event: str):
    """Empty the modal card, tell the list to refresh, then close. Closing first would detach the form and cancel the swap."""
    response = toast(HttpResponse(""), message)
    trigger_client_event(response, event, {})
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(Module.USERS, Level.FULL)
def user_invite(request):
    if request.method != "POST":
        return render(request, "web/_user_invite.html", {"form": InviteUserForm()})
    form = InviteUserForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            services.invite_user(request.tenant, email=d["email"], first_name=d["first_name"], last_name=d["last_name"], role=d["role"],
                                 department=d["department"], create_technician=d["create_technician"], by=request.user)
            return _modal_done(f"Account created for {d['email']}", "users-changed")
        except ValidationError as e:
            form.add_error(None, e.messages[0])
    return render(request, "web/_user_invite.html", {"form": form})


# --- Roles tab ------------------------------------------------------------------------------------

# Column order from the mock's MODULES list (Recalls before Contracts), not the enum's declaration order.
ROLE_MATRIX_MODULES = [Module.EQUIPMENT, Module.WORKORDERS, Module.PM, Module.RECALLS, Module.CONTRACTS, Module.REPORTS, Module.USERS, Module.SETTINGS]

def _roles_context(request) -> dict:
    counts = services.role_user_counts(request.tenant)
    matrix = []
    for role in services.role_order(Role.objects.prefetch_related("permissions")):
        levels = {p.module: p.level for p in role.permissions.all()}
        matrix.append({"role": role, "cells": [(m.value, levels.get(m.value, Level.NONE)) for m in ROLE_MATRIX_MODULES], "users": counts.get(role.id, 0)})
    return {**_tabs_context(request, "roles"), "matrix": matrix, "modules": [(m.value, m.label) for m in ROLE_MATRIX_MODULES], "levels": Level.choices,
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


@web_view(Module.USERS, Level.FULL)
def role_new(request):
    if request.method != "POST":
        return render(request, "web/_role_new.html", {"form": NewRoleForm()})
    form = NewRoleForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            role = services.create_role(name=d["name"], description=d["description"], copy_from=d["copy_from"])
            return _modal_done(f'Role "{role.name}" created', "roles-changed")
        except ValidationError as e:
            form.add_error(None, e.messages[0])
    return render(request, "web/_role_new.html", {"form": form})
