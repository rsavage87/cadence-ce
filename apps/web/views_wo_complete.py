"""
Completing a work order from its drawer (slice 15): "Mark completed" opens a modal for the resolution and, on a PM, the checklist
results and the overall result. Views parse input, call apps.workorders.completion, and answer with the drawer.

Who may: completing needs Work orders Edit (wo_perms.can_transition into completed), checked on GET and POST; others get a 403.
A work order that cannot be completed now (not started, waiting on parts, already done, not assigned) gets the modal saying why.

The modal (#modal-card): the resolution box, and for a PM the device's current checklist (a pass / fail / N/A choice per step and
a reading box for measured steps), the overall result, and for a Fail the follow-up options (open a new repair when the device
already has one open; tag the device out while it is in service). Errors re-render it with the values kept. "Mark the rest pass"
re-renders it (a GET carrying the values) with every unanswered step set to pass. Saving answers with the drawer (HX-Retarget
#drawer), toasts, fires wo-changed (and devices-changed when the device's status or next PM moved), and closes the modal after
settle: closing it first would detach the form that sent the request, which cancels the swap and loses the other events.

results_context feeds the drawer's PM results section (_wo_results.html): a completed PM's result and its checklist as recorded,
and the links between a failed PM and its follow-up repair, both ways.
"""
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import render
from django_htmx.http import retarget, trigger_client_event

from apps.equipment.models import AssetStatus
from apps.workorders import completion
from apps.workorders import permissions as wo_perms
from apps.workorders.models import OPEN_STATUSES, PmResult, ServiceRequest, Source, WoStatus, WoType

from .decorators import web_view
from .forms_wo_complete import STEP_CHOICES, CompleteForm
from .htmx import toast
from .views import _get_wo, _render_wo_drawer

MODAL = "web/_wo_complete.html"
RESULT_CSS = {PmResult.PASS: "ok", PmResult.PASS_MINOR_REPAIR: "warn", PmResult.FAIL: "crit"}
STEP_CSS = {completion.PASS: "ok", completion.FAIL: "crit", completion.NA: "neutral"}


# --- the drawer's section ------------------------------------------------------------------------------------------------------

def results_context(request, wo) -> dict:
    """Under "pm_record", so nothing collides with the drawer's own keys. One query for a PM's follow-ups, one for a repair's PM."""
    is_pm = wo.type == WoType.PM
    recorded = is_pm and bool(wo.pm_result)
    done = wo.status in completion.DONE_STATUSES
    steps = completion.recorded_steps(wo) if recorded and done else []
    for s in steps:
        s["css"] = STEP_CSS.get(s["result"], "neutral")
    return {"pm_record": {
        "show": recorded and done,
        "reopened": recorded and wo.status in OPEN_STATUSES,
        "label": wo.get_pm_result_display() if recorded else "", "css": RESULT_CSS.get(wo.pm_result, "neutral"),
        "steps": steps,
        "follow_ups": list(wo.follow_ups.order_by("opened_on", "number")) if is_pm else [],
        "follow_up_of": wo.follow_up_of if wo.follow_up_of_id else None,
    }}


# --- the modal -----------------------------------------------------------------------------------------------------------------

def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _offers(wo) -> dict:
    """A failed PM's options, as the modal offers them: the repair a failure goes to, and whether tagging out applies."""
    if wo.type != WoType.PM:
        return {"own_repair": None, "other_repair": None, "offer_open_repair": False, "offer_tag_out": False}
    own = completion.own_open_repair(wo)
    other = None if own else completion.other_open_repair(wo)
    return {"own_repair": own, "other_repair": other, "offer_open_repair": other is not None,
            "offer_tag_out": wo.asset.status in completion.HOLDABLE, "already_out": wo.asset.status != AssetStatus.IN_SERVICE}


def _revised(form, kwargs) -> CompleteForm:
    """The checklist was revised while the modal was open: show the steps as they are now, unanswered, with their signature (so the
    next save is checked against what is on screen), keeping the resolution, the result, and the options."""
    data = form.data.copy()
    for key in [k for k in data if k.startswith(("step_", "reading_"))]:
        del data[key]
    data["signature"] = completion.checklist_signature(kwargs["steps"])
    fresh = CompleteForm(data, **kwargs)
    fresh.is_valid()
    fresh._errors.setdefault("checklist", fresh.error_class()).extend(form.errors["checklist"])
    return fresh


def _form_kwargs(wo, steps, offers) -> dict:
    return {"steps": steps, "is_pm": wo.type == WoType.PM, "offer_open_repair": offers["offer_open_repair"], "offer_tag_out": offers["offer_tag_out"]}


def _modal(request, wo, form=None, *, steps=(), offers=None, reason=""):
    if form is not None and form.is_bound:
        form.focus_first_error()
    procedure = completion.procedure_for(wo)
    return render(request, MODAL, {
        "wo": wo, "asset": wo.asset, "dm": wo.asset.device_model, "is_pm": wo.type == WoType.PM, "blocker": reason, "form": form,
        "procedure": procedure, "rows": form.rows() if form is not None else [], "results": form.result_options() if form is not None else [],
        "has_checklist": bool(steps), "step_choices": STEP_CHOICES, "reading_max": completion.READING_MAX, **(offers or {}),
        "follow_up_tech": completion.follow_up_technician(wo, wo.asset) if wo.type == WoType.PM else None,
        "requester_emailed": _requester_email(wo),
    })


def _requester_email(wo) -> bool:
    """A portal request whose requester left a work email: they may be told it is done (apps.portal.notifications)."""
    if wo.source != Source.PORTAL:
        return False
    sr = ServiceRequest.objects.filter(work_order=wo).only("requester_email").first()
    return bool(sr and sr.requester_email)


def _message(wo, done) -> str:
    if wo.type != WoType.PM:
        return f"{wo.number} completed"
    message = f"{wo.number} completed: {completion.result_note(wo.pm_result, done)}"
    if done.tagged_out:
        message += f"; {wo.asset.tag} tagged out of service"
    return message


def _saved(request, wo, done):
    response = retarget(_render_wo_drawer(request, wo), "#drawer")
    for event in ("wo-changed", *(("devices-changed",) if done.device_changed else ())):
        trigger_client_event(response, event, {})
    toast(response, _message(wo, done))
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def wo_complete(request, number):
    # In progress is the one status a work order is completed from; whether it is in progress now is the modal's to say.
    _require(wo_perms.can_transition(request.user, WoStatus.IN_PROGRESS, WoStatus.COMPLETED))
    wo = _get_wo(number)  # tenant-scoped: another facility's number is a 404
    today = date.today()
    reason = completion.blocker(wo, today)
    if reason:
        return _modal(request, wo, reason=reason)
    steps = completion.checklist_of(completion.procedure_for(wo))
    offers = _offers(wo)
    kwargs = _form_kwargs(wo, steps, offers)
    if request.method != "POST":
        form = CompleteForm.filled(request.GET, fill=request.GET.get("fill", ""), **kwargs) if "fill" in request.GET else CompleteForm(**kwargs)
        return _modal(request, wo, form, steps=steps, offers=offers)
    form = CompleteForm(request.POST, **kwargs)
    done = None
    if form.is_valid():
        try:
            done = completion.complete_work_order(wo, by=request.user, today=today, **form.service_kwargs())
        except ValidationError as e:
            form.add_service_errors(e)
    if done is None:
        wo = _get_wo(number)  # as it is now: a refusal for its state (completed meanwhile) re-renders as the blocker
        reason = completion.blocker(wo, today)
        if not reason and "checklist" in form.errors:
            form = _revised(form, kwargs)
        return _modal(request, wo, None if reason else form, steps=steps, offers=offers, reason=reason)
    return _saved(request, wo, done)  # the service refreshed wo
