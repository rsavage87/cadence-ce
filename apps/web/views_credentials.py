"""
Users and access: Technician credentials tab (slice 5). One panel per technician on the left, coverage by category on the right.

The body (#creds-body) is a partial that refetches on `credentials-changed`; row actions post back and swap the body directly.
"""
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django_htmx.http import trigger_client_event

from apps.accounts.models import Level, Module, Role, User
from apps.credentials import services as cred_services
from apps.credentials.models import Credential
from apps.credentials.services import SCOPE_SHORT, coverage_by_category

from .decorators import web_view
from .forms_credentials import CredentialForm, active_technicians, technician_filter
from .htmx import is_partial, toast


def _users_summary(request) -> dict:
    # User is not tenant-scoped; the tenant filter is explicit.
    return {"active_users": User.objects.filter(tenant=request.tenant, is_active=True).count(), "roles": Role.objects.count(),
            "technicians": active_technicians().count()}


def _credentials_context(request) -> dict:
    filtered, selected = technician_filter(request.GET)
    techs = active_technicians().filter(pk=selected.pk) if selected else active_technicians().none() if filtered else active_technicians()
    panels = []
    for t in techs.prefetch_related("credentials"):
        creds = list(t.credentials.all())
        panels.append({"tech": t, "creds": creds, "active": sum(c.status == Credential.Status.ACTIVE for c in creds)})
    return {"nav_active": "users", "users_tab": "credentials", "users_summary": _users_summary(request),
            "can_manage_users": request.user.has_level(Module.USERS, Level.FULL), "can_manage_credentials": request.user.has_level(Module.USERS, Level.EDIT),
            "panels": panels, "filtered": filtered, "selected": selected, "coverage": coverage_by_category()}


@web_view(Module.USERS, Level.VIEW)
def credentials(request):
    ctx = _credentials_context(request)
    if is_partial(request, "creds-body"):
        return render(request, "web/_credentials_body.html", ctx)
    return render(request, "web/credentials.html", ctx)


def _render_body(request, message: str):
    return toast(render(request, "web/_credentials_body.html", _credentials_context(request)), message)


def _get_credential(pk):
    return get_object_or_404(Credential.objects.select_related("technician"), pk=pk)


@require_POST
@web_view(Module.USERS, Level.EDIT)
def credential_renew(request, pk):
    cred = _get_credential(pk)
    try:
        cred_services.renew_credential(cred)
        message = f"Renewed through {cred.expires_on:%b %Y}"
    except ValidationError as e:
        message = e.messages[0]
    return _render_body(request, message)


@require_POST
@web_view(Module.USERS, Level.EDIT)
def credential_sign_off(request, pk):
    cred = _get_credential(pk)
    try:
        cred_services.sign_off_credential(cred)
        message = f"Signed off: {cred.value}"
    except ValidationError as e:
        message = e.messages[0]
    return _render_body(request, message)


@require_POST
@web_view(Module.USERS, Level.EDIT)
def credential_remove(request, pk):
    cred = _get_credential(pk)
    cred_services.remove_credential(cred)
    return _render_body(request, f"Credential removed: {cred.value}")


@web_view(Module.USERS, Level.EDIT)
def credential_new(request):
    if request.method != "POST":
        _, selected = technician_filter(request.GET)
        form = CredentialForm(initial={"technician": str(selected.pk)} if selected else None)
        return render(request, "web/_credential_new.html", {"form": form})
    form = CredentialForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        scope, value = d["covers"]
        try:
            cred_services.add_credential(d["technician"], scope=scope, value=value, source=d["source"], status=d["status"],
                                         issued_on=d["issued_on"], expires_on=d["expires_on"])
        except ValidationError as e:
            form.add_error(None, e)
    if not form.is_valid():
        return render(request, "web/_credential_new.html", {"form": form})
    # The body refetches itself on credentials-changed, keeping any ?technician= filter it was opened with.
    response = trigger_client_event(HttpResponse(""), "credentials-changed", {})
    toast(response, f"{d['technician'].name}: {SCOPE_SHORT[scope].lower()} credential for {value} added")
    # After settle: closing the modal detaches the form that sent this request, which would cancel the swap and lose the other events.
    return trigger_client_event(response, "modal-close", {}, after="settle")

