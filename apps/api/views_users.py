"""
Users and access (slice 19, part D): users, roles, technicians, and their credentials, through the same doors as the Users and
access screen (apps/web/views_users.py, apps/web/views_credentials.py). Every write goes through apps.accounts.services,
apps.accounts.invitations, or apps.credentials.services; none saves a row through a serializer.

Levels, on the Users and access module ("users"): reading needs View. Users and roles change at Full, as on the Users and Roles
tabs; credentials at Edit, as on the Technician credentials tab. Scoped users (apps.workorders.scoping) are refused by every
endpoint here, whatever their levels (none names scoped_actions), and a superuser who has not picked a facility is told to pick one.
Another facility's user, role, technician, or credential in the URL is a 404, and in a body it is refused as a choice (400). A
service's refusal is a 400 in its own words, keyed by field where it names one. A write body's unknown fields are refused (400),
never dropped; a field a GET shows may be sent back as it reads, and sent changed it is refused with where it changes instead.

Users (User is not tenant-scoped: every lookup here filters on the request's facility)
  GET    /api/v1/users/                     View. ?q= (name, email, department, company), ?role=<role slug>,
                                            ?status=active|invited|deactivated (an unknown role or status is a 400). Each user: id,
                                            name, first_name, last_name, email, username, role (its id), role_name, role_slug, scope
                                            (what they see by: facility, company, or department), company (only where the role sees
                                            by company, else null), department, scope_gap ("company" or "department" while a scoped
                                            user sees nothing, else null), status (active, invited, deactivated), invitation_pending,
                                            last_sign_in, is_superuser, technician (their profile's id, or null). Never a password
                                            hash, an API token, or an invitation link.
  GET    /api/v1/users/{id}/                View.
  POST   /api/v1/users/                     Full. Invite user: {"email", "first_name", "last_name", "role": <role id>, "company",
                                            "department", "create_technician": false}. invite_user, then send_invitation, as the
                                            modal: 201 with the user, "invitation_sent", and "message". A failed email is reported
                                            there ("invitation_sent": false), never raised: the account stands, and resend-invite
                                            tries again. A role that sees by company needs the company (and only such a role takes
                                            one); a department-scoped role needs one of the facility's departments; any other role
                                            takes a department from the modal's list (the facility's, Clinical Engineering, Finance,
                                            Quality and Patient Safety, External vendor).
  PATCH  /api/v1/users/{id}/                Full. Edit on the row: {"role": <role id>, "company", "department"}, any of them (one left
                                            out keeps the account's), through set_user_access with the same rules: no one changes
                                            their own role, a move to a scoped role brings the company or unit it needs, a scoped
                                            user keeps one. PUT is not offered.
  POST   /api/v1/users/{id}/deactivate/     Full. deactivate_user: not your own account, not a superuser (managed in Admin). A
                                            deactivated user's session and API token stop working. No body.
  POST   /api/v1/users/{id}/reactivate/     Full. reactivate_user. No body.
  POST   /api/v1/users/{id}/resend-invite/  Full. send_invitation: a fresh link, and the earlier one stops working; only while the
                                            invitation is pending (400 otherwise). 200 with the user, "invitation_sent", and
                                            "message" ("invitation_sent": false when the email could not be sent). No body.

Roles
  GET    /api/v1/roles/                     View. The matrix's rows in its order (the standard roles, then custom ones by name). Each:
                                            id, name, slug, description, is_system (the Director: its levels and scope never change),
                                            standard (one of the six roles every facility starts with), levels {"<module>": 0 None,
                                            1 View, 2 Request, 3 Edit, 4 Approve, 5 Full} for every module, scope (facility, company,
                                            or department), scope_fixed (why it cannot change, or null), users (active users, invited
                                            included), users_seeing_nothing (of those, who sees nothing until a company or department
                                            is set).
  GET    /api/v1/roles/{id}/                View.
  POST   /api/v1/roles/                     Full. Add role: {"name", "copy_from": <role id>, "description", "scope"} through
                                            create_role: the copied role's levels, and the whole facility unless a scope is given. 201.
                                            Levels change afterwards with PATCH.
  PATCH  /api/v1/roles/{id}/                Full. {"levels": {"<module>": <0-5>, ...}, "scope": "facility|company|department"}, either
                                            or both, all or nothing: set_role_level for each cell that changes and set_role_scope. The
                                            Director is fixed, the default vendor and requester roles keep their scope, and no one
                                            changes their own role's levels or scope (400, keyed "levels" by module, or "scope").
                                            Sending a cell's current level, or the current scope, back changes nothing (so a row sent
                                            back as a GET returned it is fine, the Director's and your own role's included). PUT and
                                            DELETE are not offered.

Technicians (read only: no service adds or edits a technician; Invite user's create_technician adds one with the account)
  GET    /api/v1/technicians/               View. Each with their credentials.
  GET    /api/v1/technicians/{id}/          View.

Credentials (the Technician credentials tab)
  GET    /api/v1/credentials/               View. ?technician=<id>. Each: id, technician, technician_name, scope, value, source,
                                            issued_on, expires_on, status (active, in_training), state (in_training, expired,
                                            expiring, ok).
  GET    /api/v1/credentials/{id}/          View.
  POST   /api/v1/credentials/               Edit. Add credential: {"technician": <an active technician's id>, "scope":
                                            "category|manufacturer|model", "value": <a category, manufacturer, or model in the
                                            catalog>, "source": <one of the modal's: OEM training, In-house sign-off, Third-party
                                            course, Certification (CBET, CRES, CLES)>, "status": "active|in_training" (active if not
                                            given), "issued_on": "YYYY-MM-DD", "expires_on": "YYYY-MM-DD" or null}. add_credential. 201.
  POST   /api/v1/credentials/{id}/renew/    Edit. renew_credential: the expiry moves 24 months from today. No body.
  POST   /api/v1/credentials/{id}/sign-off/ Edit. sign_off_credential: an in-training credential becomes active as of today. No body.
  DELETE /api/v1/credentials/{id}/          Edit. remove_credential. 204.
  A credential is not edited in place (no PUT or PATCH), as on the tab: renew it, sign it off, or remove it and add another.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts import invitations, services
from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.accounts.services import USER_STATUSES, UserFilters
from apps.credentials import services as cred_services
from apps.credentials.models import Credential, Technician
from apps.tenants.context import get_current_tenant

from . import serializers_users as su
from .base import TenantViewSet, _via_service
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

ABSENT = object()  # a field left out of a PATCH, as opposed to one sent empty or null
NOT_DATA = {"csrfmiddlewaretoken"}  # the browsable API's HTML forms post it with the fields; Django's CSRF check reads it, not the view


class FacilityRequired:
    """A superuser who has not picked a facility would otherwise read and write nobody's rows (or, for User, which is not
    tenant-scoped, every account that has no facility)."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")


def _body(request):
    """The write body's fields; a JSON list or scalar has none to read."""
    if not hasattr(request.data, "keys"):
        raise DRFValidationError({"detail": "Send a JSON object."})
    return request.data


def _refuse_fields(body, writable, *, shown=None, why=None, unknown="Unknown field."):
    """Every key of `body` that is not `writable` is refused (400), never dropped. A field the GET shows (`shown`) may be sent back
    as it reads; changed, it is refused with `why` it changes elsewhere."""
    why = why or {}
    errors = {}
    for key in body:
        if key in writable or key in NOT_DATA:
            continue
        if shown is not None and key in shown and body[key] == shown[key]:
            continue
        errors[key] = [why.get(key) or ("Read only." if shown is not None and key in shown else unknown)]
    if errors:
        raise DRFValidationError(errors)


def _no_body(request):
    _refuse_fields(_body(request), (), unknown="This action takes no fields.")


def _write_action(view):
    """The action whose serializer describes a write. OPTIONS asks for POST's while its own action is "metadata"; the browsable
    API's forms set the action they render for."""
    return "create" if view.action == "metadata" and view.request.method == "POST" else view.action


def _given(data, field):
    """A PATCH field as sent: ABSENT when left out, "" when sent null."""
    if field not in data:
        return ABSENT
    return "" if data[field] is None else data[field]


class UsersModule(FacilityRequired, TenantAPIMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, ModulePermission]
    module = Module.USERS
    write_level = Level.FULL  # the Users and Roles tabs change things at Users Full
    filter_backends = []  # the filters are the tab's (?q, ?role, ?status), not search and ordering


# --- users ---------------------------------------------------------------------------------------------------------------------

class UserViewSet(UsersModule):
    serializer_class = su.UserSerializer
    # Where a field the GET shows changes instead, when a PATCH sends it changed.
    ELSEWHERE = {
        "status": "Use POST /api/v1/users/{id}/deactivate/ or reactivate/ to change the status.",
        "invitation_pending": "Use POST /api/v1/users/{id}/resend-invite/ to send a fresh invitation.",
        **dict.fromkeys(("name", "first_name", "last_name", "email", "username"), "A user's name and email address are set when they are invited."),
        **dict.fromkeys(("role_name", "role_slug"), "Send the role's id as role."),
        "scope": "What a user sees comes from their role: change the role, or the role's scope (PATCH /api/v1/roles/{id}/).",
        "technician": "A technician profile is added with the invitation (create_technician).",
        "is_superuser": "Superusers are managed in Admin.",
    }

    def get_serializer_class(self):  # the browsable API's forms show what each write takes
        return {"create": su.InviteSerializer, "partial_update": su.UserAccessSerializer}.get(_write_action(self), su.UserSerializer)

    def get_queryset(self):
        tenant = get_current_tenant()
        return User.objects.filter(tenant=tenant).select_related("role") if tenant else User.objects.none()

    def _show(self, users, many=False):
        users = list(users)
        context = {**self.get_serializer_context(), "departments": services.department_names(get_current_tenant()),
                   "technicians": dict(Technician.objects.filter(user__in=[u.pk for u in users]).values_list("user_id", "id"))}
        data = su.UserSerializer(users, many=True, context=context).data
        return data if many else data[0]

    def _filters(self, params) -> UserFilters:
        role, state = params.get("role", ""), params.get("status", "")
        errors = {}
        if role and not Role.objects.filter(slug=role).exists():
            errors["role"] = [f"No role here has the slug {role}."]
        if state and state not in {k for k, _ in USER_STATUSES}:
            errors["status"] = [f"One of {', '.join(k for k, _ in USER_STATUSES)}."]
        if errors:
            raise DRFValidationError(errors)
        return UserFilters(q=params.get("q", "").strip()[:100], role=role, status=state)

    def list(self, request):
        users = services.list_users(get_current_tenant(), self._filters(request.query_params))
        page = self.paginate_queryset(users)
        if page is None:
            return Response(self._show(users, many=True))
        return self.get_paginated_response(self._show(page, many=True))

    def retrieve(self, request, pk=None):
        return Response(self._show([self.get_object()]))

    @staticmethod
    def _scope_values(role, company, department, user=None):
        """The company and department the Invite user and Edit forms would send for `role` (forms_users.ScopeFieldsMixin): the
        company only for a role that sees by company (another role's form has no company field, so one sent is refused rather than
        dropped), and for a role that does not see by department, a department from the forms' list (or the account's own). None
        keeps the account's. The services check the rest (a company-scoped role's company, a department-scoped role's unit)."""
        scope = role.effective_scope if role is not None else DataScope.FACILITY
        errors = {}
        if scope != DataScope.COMPANY:
            if company is not ABSENT and str(company).strip():
                sees = DataScope(scope).label.lower()
                errors["company"] = [f"Only a role that sees its company's work orders takes a company; the {role.name} role sees {sees}."
                                     if role is not None else "Only a role that sees its company's work orders takes a company."]
            company = None
        elif company is ABSENT:
            company = None
        if department is ABSENT:
            department = None
        else:
            department = str(department).strip()
            own = (user.department or "").strip() if user is not None else ""
            if scope != DataScope.DEPARTMENT and department and department != own and department not in services.department_options():
                errors["department"] = ["Choose a department from the list: one of this facility's departments, Clinical Engineering, "
                                        "Finance, Quality and Patient Safety, or External vendor."]
        if errors:
            raise DRFValidationError(errors)
        return company, department

    def create(self, request):
        """Invite user."""
        body = _body(request)
        parsed = su.InviteSerializer(data=body)
        _refuse_fields(body, parsed.fields)
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        company, department = self._scope_values(d["role"], _given(d, "company"), _given(d, "department"))
        user = _via_service(services.invite_user, get_current_tenant(), email=d["email"], first_name=d["first_name"], last_name=d["last_name"],
                            role=d["role"], company=company, department=department, create_technician=d["create_technician"], by=request.user)
        # The account exists whatever happens to the email (sent outside invite_user's transaction); a failure is reported, not raised.
        sent = invitations.send_invitation(user, by=request.user)
        message = (f"Invitation sent to {user.email}" if sent else
                   f"The account for {user.email} was created, but the invitation email could not be sent. Once email is working, use "
                   f"POST /api/v1/users/{user.pk}/resend-invite/.")
        return Response({**self._show([user]), "invitation_sent": sent, "message": message}, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """Edit: the role, company, and department in one change (set_user_access)."""
        user = self.get_object()
        body = _body(request)
        parsed = su.UserAccessSerializer(data=body, partial=True)
        _refuse_fields(body, parsed.fields, shown=self._show([user]), why=self.ELSEWHERE)
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        role = d.get("role")  # None when left out (null is refused: "Choose a role.")
        chosen = role or (user.role if user.role_id else None)
        company, department = self._scope_values(chosen, _given(d, "company"), _given(d, "department"), user=user)
        _via_service(services.set_user_access, user, role=role, company=company, department=department, by=request.user)
        return Response(self._show([user]))

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        _no_body(request)
        _via_service(services.deactivate_user, user, by=request.user)
        return Response(self._show([user]))

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        user = self.get_object()
        _no_body(request)
        _via_service(services.reactivate_user, user, by=request.user)
        return Response(self._show([user]))

    @action(detail=True, methods=["post"], url_path="resend-invite", url_name="resend-invite")
    def resend_invite(self, request, pk=None):
        user = self.get_object()
        _no_body(request)
        sent = _via_service(invitations.send_invitation, user, by=request.user)
        message = (f"Invitation resent to {user.email}" if sent else
                   f"The invitation email to {user.email} could not be sent. Try again once email is working.")
        return Response({**self._show([user]), "invitation_sent": sent, "message": message})


# --- roles ---------------------------------------------------------------------------------------------------------------------

class RoleViewSet(UsersModule):
    serializer_class = su.RoleSerializer
    ELSEWHERE = {
        **dict.fromkeys(("name", "slug", "description"), "A role's name and description are set when it is added."),
        **dict.fromkeys(("is_system", "standard"), "Read only."),
        "scope_fixed": "Read only.",
        **dict.fromkeys(("users", "users_seeing_nothing"), "Users change roles with PATCH /api/v1/users/{id}/."),
    }

    def get_serializer_class(self):
        return {"create": su.NewRoleSerializer, "partial_update": su.RoleChangeSerializer}.get(_write_action(self), su.RoleSerializer)

    def get_queryset(self):
        return Role.objects.prefetch_related("permissions")

    def _show(self, roles, many=False):
        context = {**self.get_serializer_context(), "role_counts": services.role_user_counts(get_current_tenant())}
        data = su.RoleSerializer(list(roles), many=True, context=context).data
        return data if many else data[0]

    def _fresh(self, role):
        return self.get_queryset().get(pk=role.pk)  # its levels as now saved

    def list(self, request):
        roles = services.role_order(self.get_queryset())
        page = self.paginate_queryset(roles)
        if page is None:
            return Response(self._show(roles, many=True))
        return self.get_paginated_response(self._show(page, many=True))

    def retrieve(self, request, pk=None):
        return Response(self._show([self.get_object()]))

    def create(self, request):
        """Add role."""
        body = _body(request)
        parsed = su.NewRoleSerializer(data=body)
        _refuse_fields(body, parsed.fields, why={"levels": "Set this with PATCH once the role is added."})
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        role = _via_service(services.create_role, name=d["name"], description=d["description"], copy_from=d["copy_from"], scope=d["scope"],
                            by=request.user)
        return Response(self._show([self._fresh(role)]), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """The matrix row: levels cell by cell (set_role_level) and the scope (set_role_scope), all of it or none."""
        role = self.get_object()
        body = _body(request)
        parsed = su.RoleChangeSerializer(data=body, partial=True)
        _refuse_fields(body, parsed.fields, shown=self._show([role]), why=self.ELSEWHERE)
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        held = {p.module: p.level for p in role.permissions.all()}
        with transaction.atomic():
            for module, level in d.get("levels", {}).items():
                if module in Module.values and level in Level.values and held.get(module, Level.NONE) == level:
                    continue  # the cell as it is (the Director's too): nothing to change
                try:
                    services.set_role_level(role, module, level, by=request.user)
                except ValidationError as e:
                    raise DRFValidationError({"levels": {module: e.messages}}) from e
            if "scope" in d and d["scope"] != role.effective_scope:  # the scope as it is changes nothing either
                try:
                    services.set_role_scope(role, d["scope"], by=request.user)
                except ValidationError as e:
                    raise DRFValidationError({"scope": e.messages}) from e
        return Response(self._show([self._fresh(role)]))


# --- technicians and credentials -------------------------------------------------------------------------------------------------

class TechnicianViewSet(FacilityRequired, TenantViewSet):
    """Read only: no service adds or edits a technician (Invite user's create_technician adds one with the account)."""

    model, module, serializer_class = Technician, Module.USERS, su.TechnicianSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        return Technician.objects.prefetch_related("credentials")


class CredentialViewSet(FacilityRequired, TenantViewSet):
    """The Technician credentials tab: add, renew, sign off, and remove, each at Users Edit, through apps.credentials.services."""

    model, module, serializer_class = Credential, Module.USERS, su.CredentialSerializer
    write_level = delete_level = Level.EDIT  # removing is routine credential upkeep, gated like adding (as on the tab)
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_serializer_class(self):
        return su.NewCredentialSerializer if _write_action(self) == "create" else su.CredentialSerializer

    def get_queryset(self):
        qs = Credential.objects.select_related("technician")
        raw = self.request.query_params.get("technician") if self.action == "list" else None
        if raw:
            try:
                qs = qs.filter(technician_id=uuid.UUID(raw))
            except ValueError:
                raise DRFValidationError({"technician": ["A technician's id."]}) from None
        return qs

    def create(self, request, *args, **kwargs):
        """Add credential."""
        body = _body(request)
        parsed = su.NewCredentialSerializer(data=body)
        _refuse_fields(body, parsed.fields)
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        cred = _via_service(cred_services.add_credential, d["technician"], scope=d["scope"], value=d["value"], source=d["source"], status=d["status"],
                            issued_on=d["issued_on"], expires_on=d["expires_on"])
        return Response(su.CredentialSerializer(cred, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        cred_services.remove_credential(instance)

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        cred = self.get_object()
        _no_body(request)
        _via_service(cred_services.renew_credential, cred)
        return Response(self.get_serializer(cred).data)

    @action(detail=True, methods=["post"], url_path="sign-off", url_name="sign-off")
    def sign_off(self, request, pk=None):
        cred = self.get_object()
        _no_body(request)
        _via_service(cred_services.sign_off_credential, cred)
        return Response(self.get_serializer(cred).data)


def register(router):
    router.register("users", UserViewSet, basename="user")
    router.register("roles", RoleViewSet, basename="role")
    router.register("technicians", TechnicianViewSet, basename="technician")
    router.register("credentials", CredentialViewSet, basename="credential")
