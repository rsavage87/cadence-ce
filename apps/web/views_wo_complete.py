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

Slice 16: a scoped user (apps.workorders.scoping) completes the work orders in their share; another is a 404. A repair outside it (a
vendor's failed PM on a device whose contract leaves repairs out opens a repair for CE to assign) is neither linked nor named to
them: not in the drawer's section, the modal's failed-PM options, or the toast; where completion.py wrote its number into the PM's
resolution and history, the drawer reads "another work order" (templatetags/scoping_tags).

Slice 24, completing on a phone: a PM still open is completed in one step (the service starts it first, for someone who may start
and complete it: completion.starts_on_completion), so the drawer offers Mark completed on it next to Start work (drawer_actions). An
optional Hours box logs time with the completion (today, for costs.default_technician, in the same transaction; a refusal is shown
in the modal and nothing is saved). Opened from a My work card (from=my_work, kept as a hidden field), a save leaves the technician
on My work: no drawer (HX-Reswap none), only the toast, wo-changed (the list re-fetches itself), and the modal's close; a failed PM
still answers with its drawer, which names the repair it opened. Each step's Pass / Fail / N/A is a label filling its cell (a row
of three 44px buttons on a phone), and a reading box brings up the number pad when its step reads a number.

Slice 25, why a PM was late (the survey binder): completing a PM after its due date (completion.completes_late) offers an optional
"Why was it late?" select (late_offer): before the result for a life-support or high-risk PM, prominent, since the binder lists such a
PM until a reason is recorded; after the hours for the others. Blank keeps a reason already recorded. The drawer's "Why late" row
(views_wo_late, on a PM that missed its due date) comes in through results_context, which the drawer already calls.

Slice 26, incoming inspections (apps.workorders.inspections): an inspection is completed like a PM, in one step from open, with its
checklist (completion.procedure_for: the model's PM procedure when it has steps, else the incoming checklist; every step or none) and
its result, Passed or Failed, each saying what it does to the device (_inspection_hints): required while the device waits for its
inspection, optional otherwise (it then moves nothing). Shown once Failed is chosen, a waiting device's "If it fails": the
re-inspection it opens (due REINSPECTION_DUE_DAYS later, to completion.reinspection_assignee) or the one already open it is recorded
on, and, for a device put in use before its inspection, the tag-out. An inspection that passed and whose device no longer waits
keeps Passed (chosen to begin with; the service refuses Failed in words). The toast says what moved: in service and the first PM's
date, or the re-inspection (its number only when the user may see it, as a repair's). From My work, a failed inspection answers with
its drawer, as a failed PM does. results_context words the drawer's section by type: an inspection's result, who inspected it and
when, its steps, its re-inspection, and "Opened from failed incoming inspection".
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django_htmx.http import reswap, retarget, trigger_client_event

from apps.equipment.models import AssetStatus
from apps.workorders import completion, costs, inspections, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, InspectionResult, PmResult, ServiceRequest, Source, WoStatus, WoType

from . import views_wo_late
from .decorators import web_view
from .forms_wo_complete import STEP_CHOICES, CompleteForm
from .htmx import toast
from .views import _render_wo_drawer, get_wo
from .views_print import inspector

MODAL = "web/_wo_complete.html"
RESULT_CSS = {PmResult.PASS: "ok", PmResult.PASS_MINOR_REPAIR: "warn", PmResult.FAIL: "crit",
              InspectionResult.PASSED: "ok", InspectionResult.FAILED: "crit"}  # slice 26: the two sets of values never overlap
STEP_CSS = {completion.PASS: "ok", completion.FAIL: "crit", completion.NA: "neutral"}
NO_RESULT = "No result recorded"  # an inspection of a device that was not waiting, completed with its checklist and no result
FROM_MY_WORK = "my_work"  # the modal opened from a My work card: ?from=my_work, then a hidden field the form posts


# --- the drawer's footer (slice 24) --------------------------------------------------------------------------------------------

def drawer_actions(user, wo, actions: list[dict]) -> list[dict]:
    """The drawer's moves (views._wo_drawer_context), with Mark completed first on a PM still open that the user may start and
    complete in one step (completion.starts_on_completion). Start work stays, for a PM done over more than one visit. No moves (an
    unassigned work order, a user who may not start it): none added."""
    if not actions or not completion.starts_on_completion(wo, user):
        return actions
    return [{"to": WoStatus.COMPLETED, "label": "Mark completed", "primary": True}, *({**a, "primary": False} for a in actions)]


# --- the drawer's section ------------------------------------------------------------------------------------------------------

def results_context(request, wo) -> dict:
    """Under "pm_record", so nothing collides with the drawer's own keys. One query for a PM's follow-ups, one for a repair's PM.
    Only the ones in the user's share (apps.workorders.scoping): a link out of it would be a 404, and its number is not theirs.
    Slice 26: an incoming inspection's result (or, on one of a device that was not waiting, its checklist without one), who inspected
    it and when, and its re-inspection; a work order opened from another names it by that one's type (a failed PM or inspection)."""
    is_pm, is_inspection = wo.type == WoType.PM, wo.type == WoType.INSPECTION
    follow_up_of = wo.follow_up_of if wo.follow_up_of_id else None
    result = wo.inspection_result if is_inspection else wo.pm_result if is_pm else ""
    recorded = bool(result) or (is_inspection and bool(wo.checklist_results))
    done = wo.status in completion.DONE_STATUSES
    steps = completion.recorded_steps(wo) if recorded and done else []
    for s in steps:
        s["css"] = STEP_CSS.get(s["result"], "neutral")
    label = (wo.get_inspection_result_display() or NO_RESULT) if is_inspection else wo.get_pm_result_display() if is_pm else ""
    return {"pm_record": {
        "kind": "inspection" if is_inspection else "pm" if is_pm else "",
        "show": recorded and done,
        "reopened": recorded and wo.status in OPEN_STATUSES,
        # An inspection that passed, of a device that no longer waits: completing it again keeps it passed (completion's rule)
        "kept_pass": is_inspection and result == InspectionResult.PASSED and not wo.asset.awaiting_inspection,
        "label": label if recorded else "", "css": RESULT_CSS.get(result, "neutral"),
        "inspector": inspector(wo) if is_inspection and recorded else "",
        "steps": steps,
        "follow_ups": list(scoping.work_orders(request.user, wo.follow_ups.order_by("opened_on", "number"))) if is_pm or is_inspection else [],
        "follow_up_of": follow_up_of if follow_up_of is not None and scoping.can_see_work_order(request.user, follow_up_of) else None,
        "follow_up_of_kind": "incoming inspection" if follow_up_of is not None and follow_up_of.type == WoType.INSPECTION else "PM",
    }, **views_wo_late.late_context(request, wo)}  # slice 25: the "Why late" row (web/_wo_late.html), under "why_late"


# --- the modal -----------------------------------------------------------------------------------------------------------------

def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _number(user, wo) -> str:
    """A work order's number for this user: blank when it is outside a scoped user's share (the modal and toast then say "a repair")."""
    return wo.number if wo is not None and scoping.can_see_work_order(user, wo) else ""


def _offers(wo, user) -> dict:
    """A failed PM's options, as the modal offers them: the repair a failure goes to, and whether tagging out applies. Slice 26, an
    incoming inspection's (_inspection_offers)."""
    if wo.type == WoType.INSPECTION:
        return _inspection_offers(wo, user)
    if wo.type != WoType.PM:
        return {"own_repair": None, "other_repair": None, "offer_open_repair": False, "offer_tag_out": False}
    own = completion.own_open_repair(wo)  # the PM's own repair whoever completes it (its number only if in their share: _number)
    other = None if own else completion.other_open_repair(wo, user)
    return {"own_repair": own, "other_repair": other, "offer_open_repair": other is not None,
            "own_repair_number": _number(user, own), "other_repair_number": _number(user, other),
            "offer_tag_out": wo.asset.status in completion.HOLDABLE, "already_out": wo.asset.status != AssetStatus.IN_SERVICE}


def _inspection_offers(wo, user) -> dict:
    """An incoming inspection's options (slice 26), from the rules the completion uses. Its result is required while the device waits
    for its inspection (result_required), and only then does a fail open (or record on) a re-inspection: the one already open
    (completion.open_reinspection, its number only when the user may see it), else a new one for completion.reinspection_assignee.
    The tag-out is offered for a waiting device in use (use_before_inspection): a fail takes it out unless unticked. `kept_pass`: it
    passed and the device no longer waits, so it stays passed (Failed is refused)."""
    asset = wo.asset
    waiting = asset.awaiting_inspection
    existing = completion.open_reinspection(wo) if waiting else None
    vendor, tech = completion.reinspection_assignee(wo) if waiting and existing is None else ("", None)
    return {"own_repair": None, "other_repair": None, "offer_open_repair": False,
            "offer_tag_out": waiting and asset.status in completion.IN_USE, "result_required": waiting,
            "kept_pass": wo.inspection_result == InspectionResult.PASSED and not waiting,
            "reinspection": existing, "reinspection_number": _number(user, existing), "reinspection_vendor": vendor, "reinspection_tech": tech,
            "reinspection_days": inspections.REINSPECTION_DUE_DAYS, "inspection_hints": _inspection_hints(wo, existing)}


def _inspection_hints(wo, existing) -> dict:
    """What Passed and Failed each do to this device, under each choice (slice 26). Nothing, on a device that is not waiting."""
    asset = wo.asset
    if not asset.awaiting_inspection:
        return {InspectionResult.PASSED: "Every check passed", InspectionResult.FAILED: "A check failed: say which in the resolution"}
    if asset.status in completion.IN_USE:
        passed = "Stays in use and its PMs start"
    elif asset.status == AssetStatus.OUT_OF_SERVICE and wo_services.holding_repairs(asset).exists():
        passed = "Its PMs start; it stays out of service until its open repair is done"
    elif asset.status == AssetStatus.OUT_OF_SERVICE:
        passed = "Goes into service and its PMs start"
    elif asset.status == AssetStatus.RETIRED:  # the service starts no PM clock on a retired device
        passed = "Stops waiting for its inspection; it stays retired"
    else:
        passed = f"Its PMs start; it stays {asset.get_status_display().lower()}"
    failed = "a re-inspection opens" if existing is None else "recorded on the re-inspection open"
    if asset.status in completion.IN_USE:  # taken out of use unless the tag-out is unticked (the modal's "If it fails")
        failed = failed[0].upper() + failed[1:]
    else:
        failed = f"Stays {asset.get_status_display().lower()}; {failed}"
    return {InspectionResult.PASSED: passed, InspectionResult.FAILED: failed}


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


def _form_kwargs(wo, steps, offers, hours, late) -> dict:
    offer = late["late_offer"]
    return {"steps": steps, "is_pm": wo.type == WoType.PM, "offer_open_repair": offers["offer_open_repair"], "offer_tag_out": offers["offer_tag_out"],
            "offer_hours": hours["offer_hours"], "offer_late_reason": offer is not None, "late_first": bool(offer and offer["prominent"]),
            "late_recorded": offer["recorded"] if offer else "",
            "is_inspection": wo.type == WoType.INSPECTION, "kept_pass": offers.get("kept_pass", False)}  # slice 26


def _late(request, wo, today) -> dict:
    """The "Why was it late?" select (slice 25), under "late_offer": None unless completing today finishes a PM after its due date
    (completion.completes_late), for someone who may record the reason once it is completed (Work orders Edit). Prominent (before the
    result) for a life-support or high-risk model (its class today) the binder would list, so never for one imported from the previous
    system (the binder never calls those gaps); `recorded` names a reason already on record (blank keeps it)."""
    if not completion.completes_late(wo, today) or not request.user.has_level(wo_perms.MODULE, wo_perms.late_reason_level(WoStatus.COMPLETED)):
        return {"late_offer": None}
    return {"late_offer": {"prominent": views_wo_late.flagged_class(wo) and wo.source != Source.IMPORTED, "due_on": wo.due_on,
                           "days": (today - wo.due_on).days, "recorded": wo.get_late_reason_display() if wo.late_reason else ""}}


def _hours(request, wo) -> dict:
    """The optional Hours box (slice 24): offered to whoever may log time when the line has someone to go to (costs.default_technician:
    the technician assigned, else the user's own profile; vendor service is the vendor's time, logged without one). Logged today, at
    the Settings rate; another day, another technician, or another rate is the drawer's Log time."""
    tech = None if wo.vendor_service else costs.default_technician(wo, request.user)
    offer = wo_perms.can_record_work(request.user) and not costs.locked_reason(wo) and (wo.vendor_service or tech is not None)
    return {"offer_hours": offer, "hours_for": tech, "hours_rate": costs.default_rate(wo) if offer else None}


def from_my_work(request) -> bool:
    """Whether the modal was opened from a My work card (the GET that opens it, then the hidden field it posts and sends back)."""
    return (request.POST if request.method == "POST" else request.GET).get("from") == FROM_MY_WORK


def _modal(request, wo, form=None, *, steps=(), offers=None, hours=None, late=None, reason=""):
    if form is not None and form.is_bound:
        form.focus_first_error()
    procedure = completion.procedure_for(wo)
    is_inspection = wo.type == WoType.INSPECTION
    response = render(request, MODAL, {
        "wo": wo, "asset": wo.asset, "dm": wo.asset.device_model, "is_pm": wo.type == WoType.PM, "blocker": reason, "form": form,
        "procedure": procedure, "rows": form.rows() if form is not None else [], "results": form.result_options() if form is not None else [],
        # Slice 26: an incoming inspection's checklist (the incoming checklist when the model's procedure has no steps) and result
        "is_inspection": is_inspection, "incoming_checklist": inspections.is_incoming_checklist(procedure),
        "inspection_results": form.inspection_options((offers or {}).get("inspection_hints")) if form is not None and is_inspection else [],
        "has_checklist": bool(steps), "step_choices": STEP_CHOICES, "reading_max": completion.READING_MAX, **(offers or {}),
        **_follow_up(wo),
        "requester_emailed": _requester_email(wo),
        # Slice 24: an open PM is started as it is completed; the Hours box; where the modal was opened from
        "starting": not reason and wo.status == WoStatus.OPEN, **(hours or {"offer_hours": False}),
        "from_my_work": from_my_work(request), "from_value": FROM_MY_WORK,
        **(late if late is not None and form is not None else {"late_offer": None}),  # slice 25: "Why was it late?"
    })
    if reason and from_my_work(request):  # review fix: a card offered a move the work order no longer allows; the list is behind
        trigger_client_event(response, "wo-changed", {})
    return response


def _follow_up(wo) -> dict:
    """Who a failed PM's repair would go to, from the rule the completion uses (completion.follow_up_assignee)."""
    if wo.type != WoType.PM:
        return {"follow_up_vendor": "", "follow_up_tech": None}
    vendor, tech = completion.follow_up_assignee(wo, wo.asset)
    return {"follow_up_vendor": vendor, "follow_up_tech": tech}


def _requester_email(wo) -> bool:
    """A portal request whose requester left a work email: they may be told it is done (apps.portal.notifications)."""
    if wo.source != Source.PORTAL:
        return False
    sr = ServiceRequest.objects.filter(work_order=wo).only("requester_email").first()
    return bool(sr and sr.requester_email)


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _inspection_message(request, wo, done) -> str:
    """An incoming inspection's toast (slice 26): its result, what that did to the device, and the re-inspection (its number only
    when the user may see it)."""
    result = wo.inspection_result
    if not result:
        return f"{wo.number} completed"
    note = completion.result_note(result, done)
    if done.reinspection is not None and not _number(request.user, done.reinspection):
        where = "a re-inspection opened" if done.reinspection_opened else "a re-inspection was already open"
        note = f"{completion.RESULT_NOTES[result]}; {where}"
    message = f"{wo.number} completed: {note}"
    asset = wo.asset  # read again: complete_work_order refreshed the work order, which drops the device it had
    if done.passed:
        if asset.status == AssetStatus.IN_SERVICE:
            message += f"; {asset.tag} in service"
        elif asset.status == AssetStatus.OUT_OF_SERVICE:
            message += f"; {asset.tag} stays out of service until its open repair is done"
        if asset.next_pm_on:
            message += f"{',' if asset.status == AssetStatus.IN_SERVICE else ';'} first PM due {_day(asset.next_pm_on)}"
    if done.tagged_out:
        message += f"; {asset.tag} tagged out of service"
    return message


def _message(request, wo, done, late_given: bool = False) -> str:
    logged = f"; {costs.plain(done.labor.hours)} h logged" if done.labor is not None else ""
    if wo.type == WoType.INSPECTION:
        return _inspection_message(request, wo, done) + logged
    if wo.type != WoType.PM:
        return f"{wo.number} completed{logged}"
    note = completion.result_note(wo.pm_result, done)
    if done.repair is not None and not _number(request.user, done.repair):
        # A repair outside the user's share: say what happened without its number.
        where = "a repair work order opened" if done.follow_up else "recorded on the device's open repair"
        note = f"{completion.RESULT_NOTES.get(wo.pm_result, '')}; {where}"
    message = f"{wo.number} completed: {note}"
    if done.tagged_out:
        message += f"; {wo.asset.tag} tagged out of service"
    if late_given and wo.late_reason:
        message += f"; why late: {wo.get_late_reason_display()}"
    return message + logged


def _saved(request, wo, done, late_given: bool = False):
    """The drawer, as the work order is now; from My work (slice 24) nothing is swapped, so the technician stays on the list, which
    re-fetches itself on wo-changed. A failed PM shows its drawer either way: it names the repair it opened; so does a failed incoming
    inspection (slice 26), its re-inspection. `late_given`: a reason for lateness came with it (slice 25), which the toast names."""
    if from_my_work(request) and wo.pm_result != PmResult.FAIL and wo.inspection_result != InspectionResult.FAILED:
        response = reswap(HttpResponse(), "none")
    else:
        response = retarget(_render_wo_drawer(request, wo), "#drawer")
    for event in ("wo-changed", *(("devices-changed",) if done.device_changed else ())):
        trigger_client_event(response, event, {})
    toast(response, _message(request, wo, done, late_given))
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL, scoped=True)
def wo_complete(request, number):
    # A work order is completed from in progress, and (slice 24) a PM from open, started as it is completed by someone who may also
    # start it (completion.blocker checks that move too). Whether it can be completed now is the modal's to say.
    _require(wo_perms.can_transition(request.user, WoStatus.IN_PROGRESS, WoStatus.COMPLETED))
    wo = get_wo(request, number)  # another facility's number, or one outside a scoped user's share, is a 404
    today = timezone.localdate()
    reason = completion.blocker(wo, today, request.user)
    if reason:
        return _modal(request, wo, reason=reason)
    steps = completion.checklist_of(completion.procedure_for(wo))
    offers = _offers(wo, request.user)
    hours = _hours(request, wo)
    late = _late(request, wo, today)  # slice 25
    kwargs = _form_kwargs(wo, steps, offers, hours, late)
    if request.method != "POST":
        form = CompleteForm.filled(request.GET, fill=request.GET.get("fill", ""), **kwargs) if "fill" in request.GET else CompleteForm(**kwargs)
        return _modal(request, wo, form, steps=steps, offers=offers, hours=hours, late=late)
    form = CompleteForm(request.POST, **kwargs)
    done = None
    if form.is_valid():
        try:
            done = completion.complete_work_order(wo, by=request.user, today=today, **form.service_kwargs())
        except ValidationError as e:
            form.add_service_errors(e)
    if done is None:
        wo = get_wo(request, number)  # as it is now: a refusal for its state (completed meanwhile) re-renders as the blocker
        reason = completion.blocker(wo, today, request.user)
        if not reason and "checklist" in form.errors:
            form = _revised(form, kwargs)
        return _modal(request, wo, None if reason else form, steps=steps, offers=offers, hours=hours, late=late, reason=reason)
    return _saved(request, wo, done, late_given=bool(form.offer_late_reason and form.cleaned_data.get("late_reason")))  # the service refreshed wo
