"""
The AEM tab of the model drawer (slice 14): the model's AEM status and history, the evidence, and proposing, approving or
rejecting, withdrawing, and ending an AEM interval. Views parse input, call apps.pm.aem, and answer with the model drawer on the
AEM tab (views_models.render_model_drawer).

Who may do what (apps/pm/permissions.py), checked here on every request, GET and POST: PM View sees the tab; PM Edit proposes; PM
Approve approves, rejects, and ends; the proposer (still holding PM Edit) or a PM Approve holder withdraws an open proposal. The
proposer never decides their own case (apps.pm.aem refuses it; the tab says why the buttons are missing).

Propose, the decision, and End AEM are modals (#modal-card): a save swaps the drawer into #drawer (HX-Retarget), toasts, fires
`models-changed` (the PM library re-fetches on it) and, when devices' next PMs moved, `devices-changed` and `wo-changed` (their open
PM work orders moved too), then closes the modal after settle. Withdraw is a button inside the drawer and answers with the drawer.

End AEM's address takes the decision in force, or for an interval on file without a recorded approval, the model itself (both are
UUIDs from separate tables, so one never stands for the other).
"""
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.equipment.models import RiskClass
from apps.facility.services import get_settings
from apps.pm import aem
from apps.pm import permissions as pm_perms
from apps.pm.models import AemDecision, AemStatus

from . import views_models
from .decorators import web_view
from .forms_aem import APPROVE, DecideForm, EndForm, ProposeForm
from .htmx import toast

TAB = "aem"


def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _months(n: int) -> str:
    return f"{n} month{'' if n == 1 else 's'}"


def _policy() -> str:
    """The facility's AEM policy text (Settings), without a closing full stop: the templates add their own."""
    return (get_settings().policy_aem or "").strip().rstrip(".")


def _get_decision(pk) -> AemDecision:
    """Tenant-scoped: another facility's decision is a 404."""
    return get_object_or_404(AemDecision.objects.select_related("device_model", "proposed_by", "decided_by"), pk=pk)


def evidence_view(ev: dict | None) -> dict | None:
    """The evidence dict (apps.pm.aem.evidence, stored as JSON) with its ISO dates as dates, for the templates."""
    if not ev:
        return None
    out = dict(ev)
    for key, as_date in (("as_of", "as_of_on"), ("since", "since_on"), ("oldest_install", "oldest_on")):
        out[as_date] = date.fromisoformat(ev[key]) if ev.get(key) else None
    return out


def aem_tab(request, dm) -> dict:
    """The AEM tab's context, under "aem" so nothing collides with the drawer's own keys."""
    user, today = request.user, date.today()
    decisions = list(AemDecision.objects.filter(device_model=dm).select_related("proposed_by", "decided_by", "ended_by"))
    approved = next((d for d in decisions if d.status == AemStatus.APPROVED), None)
    proposal = next((d for d in decisions if d.status == AemStatus.PROPOSED), None)
    ev = aem.evidence(dm, today)
    can_edit, can_decide = pm_perms.can_edit_program(user), pm_perms.can_decide_aem(user)
    is_proposer = proposal is not None and proposal.proposed_by_id == user.pk
    legacy = aem.is_legacy(dm, approved)
    blocker = aem.propose_blocker(dm, today, ev=ev, open_=proposal) if can_edit else ""
    return {"aem": {
        "oem": dm.oem_pm_interval_months, "life_support": dm.risk_class == RiskClass.LIFE_SUPPORT,
        "approved": approved, "legacy": legacy, "legacy_months": dm.aem_interval_months if legacy else None,
        # The history the proposal was made on, unless it is today's figures (shown below) unchanged
        "proposal": proposal, "proposal_evidence": evidence_view(proposal.evidence) if proposal and proposal.evidence != ev else None,
        "evidence": evidence_view(ev), "history": decisions, "history_years": aem.AEM_HISTORY_YEARS,
        "can_propose": can_edit and not blocker, "propose_note": blocker,
        "can_decide": can_decide and proposal is not None and not is_proposer,
        "decide_note": aem.decide_blocker(proposal, user) if can_decide and is_proposer else "",
        "can_withdraw": proposal is not None and (can_decide or (is_proposer and can_edit)),
        "can_end": can_decide and (approved is not None or legacy),
        "end_pk": approved.pk if approved is not None else dm.pk,
    }}


def _drawer(request, dm):
    return views_models.render_model_drawer(request, dm, tab=TAB)


def _changed(response, moved: int = 0):
    for event in ("models-changed", *(("devices-changed", "wo-changed") if moved else ())):
        trigger_client_event(response, event, {})
    return response


def _saved(request, dm, message: str, moved: int = 0):
    """A modal saved: show the model's drawer on the AEM tab, refresh what depends on it, toast, then close the modal. After
    settle: closing it first would detach the form that sent this request, which cancels the swap and loses the other events."""
    response = retarget(_drawer(request, dm), "#drawer")
    toast(_changed(response, moved), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _moved_note(moved: int) -> str:
    if not moved:
        return ""
    whose = "1 device's" if moved == 1 else f"{moved} devices'"
    return f"; {whose} next PM moved earlier"


# --- Propose ---------------------------------------------------------------------------------------------------

def _propose_modal(request, dm, form):
    today = date.today()
    ev = aem.evidence(dm, today)
    return render(request, "web/_aem_propose.html", {
        "dm": dm, "form": form, "blocker": aem.propose_blocker(dm, today, ev=ev), "evidence": evidence_view(ev),
        "policy": _policy(), "history_years": aem.AEM_HISTORY_YEARS})


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_propose(request, pk):
    _require(pm_perms.can_edit_program(request.user))
    dm = views_models.get_model(pk)
    if request.method != "POST":
        return _propose_modal(request, dm, ProposeForm())
    form = ProposeForm(request.POST)
    if form.is_valid():
        try:
            decision = aem.propose(dm, interval_months=form.cleaned_data["interval_months"], rationale=form.cleaned_data["rationale"], by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _propose_modal(request, dm, form)
    return _saved(request, dm, f"AEM of {_months(decision.interval_months)} proposed for {dm}; it goes to the committee")


# --- the committee's decision ----------------------------------------------------------------------------------

def _decide_modal(request, decision, form):
    dm = decision.device_model
    shorter = form.choice == APPROVE and decision.interval_months < dm.pm_interval_months
    return render(request, "web/_aem_decide.html", {
        "decision": decision, "dm": dm, "form": form, "blocker": aem.decide_blocker(decision, request.user),
        "evidence": evidence_view(decision.evidence), "policy": _policy(), "shorter": shorter,
        "oem_changed": aem.oem_changed_refusal(decision, dm) if decision.status == AemStatus.PROPOSED else "",
        "moves": len(aem.pull_in_plan(dm, decision.interval_months)) if shorter else 0})


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_decide(request, pk):
    _require(pm_perms.can_decide_aem(request.user))
    decision = _get_decision(pk)
    dm = decision.device_model
    if request.method != "POST":
        initial = {"decision": request.GET.get("decision") if request.GET.get("decision") in ("approve", "reject") else APPROVE,
                   "decided_on": date.today()}
        return _decide_modal(request, decision, DecideForm(initial=initial))
    form = DecideForm(request.POST)
    approved = None
    if form.is_valid():
        d = form.cleaned_data
        try:
            if d["decision"] == APPROVE:
                approved = aem.approve(decision, by=request.user, decided_on=d["decided_on"], note=d["note"])
            else:
                aem.reject(decision, by=request.user, decided_on=d["decided_on"], note=d["note"])
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _decide_modal(request, decision, form)
    if approved is None:
        return _saved(request, dm, f"AEM proposal for {dm} rejected")
    moved = approved.devices_moved
    return _saved(request, dm, f"AEM of {_months(approved.interval_months)} approved for {dm}{_moved_note(moved)}", moved)


# --- Withdraw --------------------------------------------------------------------------------------------------

@require_POST
@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_withdraw(request, pk):
    decision = _get_decision(pk)
    user = request.user
    # The proposer while they can still work on the PM program, or anyone who decides AEM cases.
    _require(pm_perms.can_decide_aem(user) or (decision.proposed_by_id == user.pk and pm_perms.can_edit_program(user)))
    dm = decision.device_model
    try:
        aem.withdraw(decision, by=user)
    except ValidationError as e:
        return toast(_drawer(request, dm), e.messages[0])
    return toast(_changed(_drawer(request, dm)), f"AEM proposal for {dm} withdrawn")


# --- End AEM ---------------------------------------------------------------------------------------------------

def _end_target(pk):
    """The approved decision named, or the model named (its interval in force, approved or on file without a recorded approval)."""
    decision = AemDecision.objects.select_related("device_model", "decided_by").filter(pk=pk).first()
    if decision is not None:
        return decision, decision.device_model
    dm = views_models.get_model(pk)
    return None, dm


def _end_modal(request, pk, decision, dm, form):
    approved = decision if decision is not None else aem.in_force(dm)
    if decision is not None and decision.status != AemStatus.APPROVED:
        blocker = "This AEM interval is no longer in force."
    elif approved is None and dm.aem_interval_months is None:
        blocker = "This model has no AEM interval in force: it follows the OEM interval."
    else:
        blocker = ""
    moves_devices = aem.end_moves_devices(dm)  # a life-support model never used the interval: ending it moves nothing
    return render(request, "web/_aem_end.html", {
        "pk": pk, "dm": dm, "approved": approved, "form": form, "blocker": blocker, "unused": not moves_devices,
        "from_months": dm.aem_interval_months or (approved.interval_months if approved is not None else None),
        "moves": 0 if blocker or not moves_devices else len(aem.pull_in_plan(dm, dm.oem_pm_interval_months))})


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_end(request, pk):
    _require(pm_perms.can_decide_aem(request.user))
    decision, dm = _end_target(pk)
    if request.method != "POST":
        return _end_modal(request, pk, decision, dm, EndForm())
    form = EndForm(request.POST)
    moved = 0
    if form.is_valid():
        try:
            moved = aem.end(decision or dm, by=request.user, reason=form.cleaned_data["reason"])
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _end_modal(request, pk, decision, dm, form)
    return _saved(request, dm, f"AEM ended for {dm}: back on the OEM interval of {_months(dm.oem_pm_interval_months)}{_moved_note(moved)}", moved)
