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
(views_wo_late, on a PM that missed its due date) comes in through results_context, which the drawer already calls. Slice 27, the PM
completion window (apps.pm.windows): "late" is after the PM's on-time window, the due date unless the facility chose otherwise. A PM
completed after its due date but inside its window is on time: no select, and a line saying so and that its next PM is set from its
due date (inside_window); past a window other than the due date, the select's hint names the window and the day it ended.

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

Slice 29, a rental's, vendor loaner's, or demo unit's incoming inspection (equipment.services.owner_maintains): its checklist is the
rental checklist (inspections.TEMPORARY_INCOMING, the incoming checklist with the owner's PM label), and the modal says so; Passed never
says its PMs start (its owner maintains it); and while its owner's PM date has passed (inspections.owner_pm_refusal, completion's
refusal of Passed) the modal says so before anything is filled in, and Passed's hint says it waits for the owner's PM.

Slice 30, checks after a repair (apps.workorders.repair_checks; CMS A-0724, NFPA 99, IEC 62353). The repair's modal is path-first:
"After the repair" (completion.repair_outcomes) as radio cards above everything else, each saying what it does to this device
(_repair_hints: back in service; stays out until another repair is done; stays held until its incident is released; stays waiting for
its incoming inspection; out of service, to be retired or returned to its owner). Checked: ready for use is chosen to begin with unless
the vendor's report is offered (vendor service, for a user who sees the whole facility), whose number box shows with that choice. The
checks after repair (completion.procedure_for) show only with Checked (CSS :has; all of it without :has support), with Mark the rest
pass, the number pad on the readings, and no N/A on the visual and function checks (repair_checks.NEVER_NA). A step marked Fail shows
"If a check fails": Keep the repair open (wo_keep_open, completion.keep_open: the failed checks as a note on the repair, and with the
hold box, ticked by default, the device held out) instead of completing it; completing it as checked with a failed step is refused at
the step. The toast says what moved (completion.repair_note; not returned to use on a device that was in use says it was tagged out
of service). From My work both answer without the drawer. results_context's "repair" kind is the drawer's "Checks after repair": the
outcome, who checked (repair_checks.checked_by) and when, the steps and readings, the vendor's report number, or not returned to use;
a reopened repair says what was recorded, and one completed with no outcome (before slice 30, or imported) says none was.

Slice 31, vendor staff (apps.vendors): vendor service of any type asks "Who did the work" (views_wo_engineer's select: the vendor's
engineers, other vendors' for CE, someone else with a typed name, or not named on the report; each option's standing for this device
today for CE, names only for a vendor's own account; the engineer stored chosen to begin with on a reopened job) and, for a user who
sees the whole facility, the vendor's report number beside it, whatever the outcome (completion requires it only with checked by the
vendor). Both go to complete_work_order (vendor_engineer / engineer_name through forms_wo_complete.engineer_kwargs, checks_reference);
neither blocks completion, and a refusal comes back keyed vendor_engineer, engineer_name, or checks_reference with the rest. Keep open
names no engineer. results_context's "Checked by" names the engineer as this user may read it (repair_checks.checked_by(wo, user)).

Slice 33, what a repair found (apps.workorders.findings): a repair's modal asks "What was found" after the checks, directly above the
resolution, whatever the outcome (views_wo_finding's select: findings.choices_for this user and repair, in findings.GROUPS; the chosen
one's hint; "What each means"; a failed PM's repair's "Found on PM <number>: choose what failed."; for a user who sees the whole
facility the device's earlier service, the evidence for "PM could have caught it" and "Caused by earlier service"). Required when none
is stored (completion's rule), so it is focused before the resolution when missing; a reopened repair's finding is prefilled for a user
who sees the whole facility, and a scoped user never sees it (their blank keeps it; one they cannot choose is "Recorded by the
facility" with no select). Refusals come back keyed repair_finding with the rest, the values kept; Mark the rest pass keeps the choice;
My work's Complete is this modal. results_context carries the drawer's "What was found" row (views_wo_finding.drawer_context, under
"what_found": a user who sees the whole facility only), and a reopened repair's note names the finding it keeps for that user.

Slice 35, the test equipment used (apps.testequipment; views_wo_test_equipment): "Test equipment used (optional)" right after the steps
(also on a type or a procedure with none), only where rules.offered holds and the facility has test equipment to offer
(views_wo_test_equipment.offer, read once in _setup): Yours first, then Other test equipment by kind, collapsed; each with its standing
judged today; nothing ticked for the user but what the work order already recorded (kept ones ticked and disabled, posted through hidden
inputs); the hidden marker shown_test_equipment (forms_wo_complete: absent keeps, shown with none ticked clears). On a repair it hides
with Checked by the vendor, and the ticks are dropped then. A refusal comes back keyed test_equipment with the rest, the ticks kept and
the first box focused when it is the first error; Mark the rest pass keeps every tick. Keep open records none (completion.keep_open).
results_context carries the drawer's "Test equipment" section (views_wo_test_equipment.drawer_context, under "test_equipment_row").

Slice 38, what a software update did (apps.cybersecurity.updates; views_wo_release): "Software on the device" after the steps and the test
equipment picker, before the late reason and the resolution, only where cybersecurity.rules.release_offer holds (views_wo_release.offer,
read once in _setup, under "release_picker"; the form's offer_software): the target in rules' words, the outcome radios (none chosen
while a choice is needed; "Keep as recorded" chosen on a re-completion with an outcome recorded), the release it runs now, the reference,
the basis, the help line, and the shown_software_outcome marker (absent: nothing sent). The modal is scoped=True: a scoped user (or one
without the offer) never sees the section, and the form takes nothing from their post. A refusal comes back keyed software_outcome,
software_release, software_reference, or software_basis with the rest, the choices kept and the first one focused. The toast adds what
was recorded in rules.update_words for a user with Device security View and Equipment View (views_wo_release.toast_words); an update
completed with nothing recorded says "<number> completed". results_context carries the drawer's "Software update" section
(views_wo_release.drawer_context, under "software_row").
"""
import copy

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone
from django_htmx.http import reswap, retarget, trigger_client_event

from apps.equipment.models import AssetStatus
from apps.equipment.services import owner_maintains, status_label
from apps.incidents import services as inc_services
from apps.pm.windows import windows
from apps.workorders import completion, costs, findings, inspections, repair_checks, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, InspectionResult, PmResult, RepairCheck, ServiceRequest, Source, WoStatus, WoType

from . import views_wo_engineer, views_wo_finding, views_wo_late, views_wo_release, views_wo_test_equipment
from .decorators import web_view
from .forms_wo_complete import STEP_CHOICES, CompleteForm
from .htmx import toast
from .views import _render_wo_drawer, get_wo
from .views_print import inspector

MODAL = "web/_wo_complete.html"
RESULT_CSS = {PmResult.PASS: "ok", PmResult.PASS_MINOR_REPAIR: "warn", PmResult.FAIL: "crit",
              InspectionResult.PASSED: "ok", InspectionResult.FAILED: "crit"}  # slice 26: the two sets of values never overlap
# Slice 30: a repair's outcome in the drawer (RepairCheck's values never overlap a PM's or an inspection's either)
REPAIR_CSS = {RepairCheck.PASSED: "ok", RepairCheck.VENDOR_REPORT: "ok", RepairCheck.NOT_RETURNED: "warn"}
NO_REPAIR_CHECK = "No checks after repair were recorded with this repair"  # completed before slice 30
IMPORTED_NO_CHECK = "Imported from the previous system: no checks after repair recorded here"
REVISIONS = (completion.REVISED, completion.REPAIR_REVISED)  # the checklist errors that show the steps as they are now, unanswered
NEVER_NA_WORDS = "steps " + " and ".join(str(i + 1) for i in repair_checks.NEVER_NA)  # "steps 1 and 5"
CYBER_CHANGED = "cyber-changed"  # slice 38: the register changed (views_cybersecurity.CHANGED): the screen's lists refetch themselves
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
    it and when, and its re-inspection; a work order opened from another names it by that one's type (a failed PM or inspection).
    Slice 30: a repair's checks after it (kind "repair"): its outcome, who checked (repair_checks.checked_by: the vendor on vendor
    service, else the technician assigned) and when, the steps as recorded or the vendor's report number; `unrecorded` words a repair
    completed with no outcome (before slice 30, or imported). Slice 33: the drawer's "What was found" row (views_wo_finding, under
    "what_found"), and a reopened repair's finding (`finding`) for a user who sees the whole facility. Slice 35: the "Test equipment"
    section (views_wo_test_equipment.drawer_context, under "test_equipment_row"). Slice 38: the "Software update" section
    (views_wo_release.drawer_context, under "software_row")."""
    is_pm, is_inspection, is_repair = wo.type == WoType.PM, wo.type == WoType.INSPECTION, wo.type == WoType.REPAIR
    follow_up_of = wo.follow_up_of if wo.follow_up_of_id else None
    result = wo.inspection_result if is_inspection else wo.pm_result if is_pm else wo.repair_check if is_repair else ""
    recorded = bool(result) or (is_inspection and bool(wo.checklist_results))
    done = wo.status in completion.DONE_STATUSES
    steps = completion.recorded_steps(wo) if recorded and done else []
    for s in steps:
        s["css"] = STEP_CSS.get(s["result"], "neutral")
    label = ((wo.get_inspection_result_display() or NO_RESULT) if is_inspection else wo.get_pm_result_display() if is_pm
             else wo.get_repair_check_display() if is_repair else "")
    return {"pm_record": {
        "kind": "inspection" if is_inspection else "pm" if is_pm else "repair" if is_repair else "",
        "show": recorded and done,
        "reopened": recorded and wo.status in OPEN_STATUSES,
        # The inspection whose pass ended the device's wait: completing it again keeps it passed (completion's rule)
        "kept_pass": is_inspection and recorded and inspections.pass_cleared_flag(wo),
        "label": label if recorded else "", "css": REPAIR_CSS.get(result, "neutral") if is_repair else RESULT_CSS.get(result, "neutral"),
        # slice 31: the vendor's engineer the work order names, as this user may read it (repair_checks.checked_by)
        "inspector": inspector(wo, request.user) if (is_inspection or is_repair) and recorded else "",
        # Slice 30: a repair completed with no outcome says so (imported ones apart)
        "unrecorded": ((IMPORTED_NO_CHECK if wo.source == Source.IMPORTED else NO_REPAIR_CHECK)
                       if is_repair and done and not recorded else ""),
        "words": completion.REPAIR_CHECK_WORDS.get(result, "") if is_repair else "",  # "checked and ready for use"
        # Review fix: a repair not returned to use says reopening it returns the device only when completing it as checked would
        # (services.returns_device asked as if checked now: never a device retired, back in use, held, waiting, or held by another repair)
        "reopen_returns": is_repair and result == RepairCheck.NOT_RETURNED and _returns_if_checked(wo),
        # Slice 33: the finding a reopened repair keeps unless another is chosen, never a scoped user's (the facility's record); only
        # while this user may keep it (review fix: one the repair's limits now refuse is chosen again, and the note never says kept)
        "finding": (findings.label(wo.repair_finding) if is_repair and wo.repair_finding and wo.status in OPEN_STATUSES
                    and not scoping.is_scoped(request.user) and wo.repair_finding in findings.choices_for(wo, request.user) else ""),
        "steps": steps,
        "follow_ups": list(scoping.work_orders(request.user, wo.follow_ups.order_by("opened_on", "number"))) if is_pm or is_inspection else [],
        "follow_up_of": follow_up_of if follow_up_of is not None and scoping.can_see_work_order(request.user, follow_up_of) else None,
        "follow_up_of_kind": "incoming inspection" if follow_up_of is not None and follow_up_of.type == WoType.INSPECTION else "PM",
    }, **views_wo_late.late_context(request, wo),  # slice 25: the "Why late" row (web/_wo_late.html), under "why_late"
        **views_wo_finding.drawer_context(request, wo),  # slice 33: the "What was found" row (web/_wo_finding_row.html), under "what_found"
        **views_wo_test_equipment.drawer_context(request, wo),  # slice 35: web/_wo_test_equipment_row.html, under "test_equipment_row"
        **views_wo_release.drawer_context(request, wo)}  # slice 38: web/_wo_release_row.html, under "software_row"


# --- the modal -----------------------------------------------------------------------------------------------------------------

def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _number(user, wo) -> str:
    """A work order's number for this user: blank when it is outside a scoped user's share (the modal and toast then say "a repair")."""
    return wo.number if wo is not None and scoping.can_see_work_order(user, wo) else ""


def _offers(wo, user, today) -> dict:
    """A failed PM's options, as the modal offers them: the repair a failure goes to, and whether tagging out applies. Slice 26, an
    incoming inspection's (_inspection_offers). Slice 30, a repair's outcomes (_repair_offers)."""
    if wo.type == WoType.INSPECTION:
        return _inspection_offers(wo, user, today)
    if wo.type == WoType.REPAIR:
        return _repair_offers(wo, user)
    if wo.type != WoType.PM:
        return {"own_repair": None, "other_repair": None, "offer_open_repair": False, "offer_tag_out": False}
    own = completion.own_open_repair(wo)  # the PM's own repair whoever completes it (its number only if in their share: _number)
    other = None if own else completion.other_open_repair(wo, user)
    return {"own_repair": own, "other_repair": other, "offer_open_repair": other is not None,
            "own_repair_number": _number(user, own), "other_repair_number": _number(user, other),
            "offer_tag_out": wo.asset.status in completion.HOLDABLE, "already_out": wo.asset.status != AssetStatus.IN_SERVICE}


def _inspection_offers(wo, user, today=None) -> dict:
    """An incoming inspection's options (slice 26), from the rules the completion uses. Its result is required while the device waits
    for its inspection (result_required), and only then does a fail open (or record on) a re-inspection: the one already open
    (completion.open_reinspection, its number only when the user may see it), else a new one for completion.reinspection_assignee.
    The tag-out is offered for a waiting device in use (use_before_inspection): a fail takes it out unless unticked. `kept_pass`: it
    passed and the device no longer waits, so it stays passed (Failed is refused)."""
    asset = wo.asset
    waiting = asset.awaiting_inspection
    existing = completion.open_reinspection(wo) if waiting else None
    vendor, tech = completion.reinspection_assignee(wo) if waiting and existing is None else ("", None)
    kept_pass = inspections.pass_cleared_flag(wo)
    today = today or timezone.localdate()  # wo_complete's day, read once there
    # Slice 29: a rental's, vendor loaner's, or demo unit's owner's PM date has passed: Passed is refused (completion's rule), said up front
    owner_pm = "" if kept_pass else inspections.owner_pm_refusal(asset, today)
    hints = _inspection_hints(wo, existing)
    if owner_pm:
        hints[InspectionResult.PASSED] = "Not until the owner's PM is done, or its new date recorded"
    return {"own_repair": None, "other_repair": None, "offer_open_repair": False,
            "offer_tag_out": waiting and asset.status in completion.IN_USE, "result_required": waiting,
            "kept_pass": kept_pass, "owner_pm_refusal": owner_pm,
            "reinspection": existing, "reinspection_number": _number(user, existing), "reinspection_vendor": vendor, "reinspection_tech": tech,
            "reinspection_days": inspections.REINSPECTION_DUE_DAYS, "inspection_hints": hints}


def _repair_offers(wo, user) -> dict:
    """A repair's outcomes as the modal offers them (slice 30): those `user` may choose (completion.repair_outcomes), each with what it
    does to the device (_repair_hints); the one chosen to begin with (checked and ready for use, unless the vendor's report is offered:
    then the user chooses); and "If a check fails" (_keep_offer). Slice 33: what was found (views_wo_finding.select_context, under
    "finding_select")."""
    outcomes = completion.repair_outcomes(wo, user)
    vendor = RepairCheck.VENDOR_REPORT in outcomes
    return {"own_repair": None, "other_repair": None, "offer_open_repair": False, "offer_tag_out": False,
            "repair_outcomes": outcomes, "offer_vendor_report": vendor, "repair_default": "" if vendor else RepairCheck.PASSED,
            "repair_hints": _repair_hints(wo, user), **_keep_offer(wo, user),
            "finding_select": views_wo_finding.select_context(wo, user)}


def _held_words(asset, user) -> str:
    """A device held as evidence (slice 28), as a hint says it to `user`: the incident's number only for Incidents View
    (incidents.services.hold_words). "" when it is not held."""
    words = inc_services.hold_words(asset, user)
    if words is None:
        return ""
    if words["number"]:
        return f"{asset.tag} stays held until incident {words['number']} is released"
    return f"{asset.tag} stays held by Clinical Engineering"


def _returns_if_checked(wo) -> bool:
    """Whether completing the repair as checked now would put its device back in service (services.returns_device on a copy)."""
    probe = copy.copy(wo)
    probe.repair_check = RepairCheck.PASSED
    return wo_services.returns_device(probe)


def _checked_effect(wo, user) -> str:
    """What completing the repair as checked (or with the vendor's report) does to its device: back in service when this repair is what
    holds it out and nothing else does (workorders.services.returns_device, asked as if it were checked now: a reopened repair may still
    read not returned to use), else why it stays as it is."""
    asset, tag = wo.asset, wo.asset.tag
    if _returns_if_checked(wo):
        return f"{tag} goes back into service"
    held = _held_words(asset, user)
    if held:
        return held
    if asset.awaiting_inspection:
        return f"{tag} stays waiting for its incoming inspection"
    other = wo_services.holding_repairs(asset, exclude=wo).order_by("opened_on", "number").first()
    if other is not None:
        number = _number(user, other)
        return f"{tag} stays out until {number} is done" if number else f"{tag} stays out until its other open repair is done"
    if asset.status == AssetStatus.OUT_OF_SERVICE:  # taken out by hand: back from its drawer
        return f"{tag} stays out of service until it is returned from its drawer"
    return f"{tag} stays {status_label(asset).lower()}"


def _not_returned_effect(wo, user) -> str:
    """What not returned to use does to the device: out of service (completion.KEEP_OUT_FROM moves it), to be retired, or, a rental's,
    vendor loaner's, or demo unit's, returned to its owner; a device held as evidence stays as its hold keeps it."""
    asset, tag = wo.asset, wo.asset.tag
    held = _held_words(asset, user)
    if held:
        return held
    then = "return it to its owner" if owner_maintains(asset) else "retire it"
    if asset.status in completion.KEEP_OUT_FROM:
        return f"{tag} goes out of service: {then}"
    if asset.status == AssetStatus.OUT_OF_SERVICE:
        return f"{tag} stays out of service: {then}"
    return f"{tag} stays {status_label(asset).lower()}"


def _repair_hints(wo, user) -> dict:
    """What each outcome does to this repair's device (slice 30), under each choice."""
    checked = _checked_effect(wo, user)
    return {RepairCheck.PASSED: f"Every check recorded, none failed; {checked}",
            RepairCheck.VENDOR_REPORT: f"Their service report's number, no steps; {checked}",
            RepairCheck.NOT_RETURNED: f"Beyond repair, to be retired, or going back; {_not_returned_effect(wo, user)}"}


def _keep_offer(wo, user) -> dict:
    """"If a check fails" (slice 30): keeping the repair open with its failed checks recorded (completion.keep_open, Work orders Edit:
    wo_perms.can_keep_open), and the hold-out box when holding changes something: a device in use is tagged out; one in repair, out of
    service, or anywhere else is held out by this repair (marked tagged out, so only its completion as checked returns it). A repair
    that already holds a device not in use offers no box (`hold_words` says so)."""
    asset, tag = wo.asset, wo.asset.tag
    offer = {"offer_keep_open": wo_perms.can_keep_open(user), "offer_hold": True, "hold_words": ""}
    if asset.status in completion.IN_USE:
        offer["hold_words"] = f"Tag {tag} out of service until the repair is checked"
    elif wo.tagged_out:
        offer.update(offer_hold=False, hold_words=f"{wo.number} already holds {tag} out of use until it is completed as checked.")
    elif asset.status == AssetStatus.IN_REPAIR:
        offer["hold_words"] = f"Hold {tag} out of use until the repair is checked (it stays in repair meanwhile)"
    elif asset.status == AssetStatus.OUT_OF_SERVICE:
        offer["hold_words"] = f"Keep {tag} out of service until the repair is checked"
    else:
        offer["hold_words"] = f"Hold {tag} out of use until the repair is checked"
    return offer


def _inspection_hints(wo, existing) -> dict:
    """What Passed and Failed each do to this device, under each choice (slice 26). Nothing, on a device that is not waiting."""
    asset = wo.asset
    if not asset.awaiting_inspection:
        return {InspectionResult.PASSED: "Every check passed", InspectionResult.FAILED: "A check failed: say which in the resolution"}
    if owner_maintains(asset):
        passed = _temporary_passed(asset)  # slice 29: no PMs of ours start
    elif asset.status in completion.IN_USE:
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


def _temporary_passed(asset) -> str:
    """What Passed does to a waiting rental, vendor loaner, or demo unit (slice 29): its owner maintains it, so no PM of ours starts
    (equipment.services.pass_incoming_inspection); it goes into use, or stays as it is."""
    if asset.status in completion.IN_USE:
        return "Stays in use; its owner maintains it"
    if asset.status == AssetStatus.OUT_OF_SERVICE and wo_services.holding_repairs(asset).exists():
        return "It stays out of service until its open repair is done"
    if asset.status == AssetStatus.OUT_OF_SERVICE and not asset.incident_hold:
        return "Goes into service; its owner maintains it"
    return f"Stops waiting for its inspection; it stays {asset.get_status_display().lower()}"


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


def _vendor_offers(wo, user, today) -> dict:
    """Slice 31, vendor service of any type: who did the work (views_wo_engineer's select, each option's standing judged today for a
    user who sees the whole facility, names only for a vendor's own account), the engineer stored to begin with (a reopened job), and
    the vendor's report number box (any vendor job, a vendor's own account too: the API takes it from them, and their report's number
    is the trail a surveyor asks for; "checked by the vendor" stays CE's, slice 30)."""
    if not wo.vendor_service:
        return {"engineer_select": None, "engineer_initial": "", "offer_reference": False, "reference_initial": ""}
    sel = views_wo_engineer.select_context(wo, user, today)
    return {"engineer_select": sel, "engineer_initial": views_wo_engineer.initial_choice(wo, sel), "offer_reference": True,
            "reference_initial": wo.checks_reference}  # a reopened job's number, as the engineer stored is


def _form_kwargs(wo, steps, offers, hours, late) -> dict:
    offer = late["late_offer"]
    return {"steps": steps, "is_pm": wo.type == WoType.PM, "offer_open_repair": offers["offer_open_repair"], "offer_tag_out": offers["offer_tag_out"],
            "offer_hours": hours["offer_hours"], "offer_late_reason": offer is not None, "late_first": bool(offer and offer["prominent"]),
            "late_recorded": offer["recorded"] if offer else "",
            "is_inspection": wo.type == WoType.INSPECTION, "kept_pass": offers.get("kept_pass", False),  # slice 26
            "is_repair": wo.type == WoType.REPAIR, "repair_default": offers.get("repair_default", ""),  # slice 30
            "offer_engineer": offers.get("engineer_select") is not None, "engineer_initial": offers.get("engineer_initial", ""),  # slice 31
            "offer_reference": offers.get("offer_reference", False), "reference_initial": offers.get("reference_initial", ""),
            "finding_initial": (offers.get("finding_select") or {}).get("initial", ""),  # slice 33
            "offer_test_equipment": offers.get("te_picker") is not None,  # slice 35
            "offer_software": offers.get("release_picker")}  # slice 38: None where nothing is offered


def _late(request, wo, today) -> dict:
    """The "Why was it late?" select (slice 25), under "late_offer": None unless completing today finishes a PM after its on-time
    window (completion.completes_late; slice 27: the facility's window by the model's class, the due date by default), for someone who
    may record the reason once it is completed (Work orders Edit). Prominent (before the result) for a life-support or high-risk model
    (its class today) the binder would list, so never for one imported from the previous system (the binder never calls those gaps);
    `recorded` names a reason already on record (blank keeps it); under a window other than the due date, `window` says it and
    `window_end` when it ended. Slice 27, under "inside_window": a PM done after its due date but inside its window, which is on time
    (no reason asked) and sets its next PM from its due date: its due date and the window's last day. The window is read once, and only
    for a PM past its due date."""
    if wo.type != WoType.PM or wo.due_on is None or today <= wo.due_on:
        return {"late_offer": None, "inside_window": None}
    w = windows()
    end = completion.inside_window(wo, today, w)
    if end is not None:
        return {"late_offer": None, "inside_window": {"due_on": wo.due_on, "end": end}}
    if not completion.completes_late(wo, today, w) or not request.user.has_level(wo_perms.MODULE, wo_perms.late_reason_level(WoStatus.COMPLETED)):
        return {"late_offer": None, "inside_window": None}
    window = completion.on_time_window(wo, w)
    words = window.describe()
    return {"late_offer": {"prominent": views_wo_late.flagged_class(wo) and wo.source != Source.IMPORTED, "due_on": wo.due_on,
                           "days": (today - wo.due_on).days, "recorded": wo.get_late_reason_display() if wo.late_reason else "",
                           "window": "" if window.is_due_date else words[0].lower() + words[1:],
                           "window_end": None if window.is_due_date else window.end(wo.due_on)},
            "inside_window": None}


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
        "rental_checklist": getattr(procedure, "temporary", False),  # slice 29: inspections.TEMPORARY_INCOMING
        "inspection_results": form.inspection_options((offers or {}).get("inspection_hints")) if form is not None and is_inspection else [],
        # Slice 30: a repair's outcomes and its checks after it
        "is_repair": wo.type == WoType.REPAIR,
        "repair_options": (form.repair_options((offers or {}).get("repair_outcomes", ()), (offers or {}).get("repair_hints"))
                           if form is not None and wo.type == WoType.REPAIR else []),
        "never_na_words": NEVER_NA_WORDS,
        "has_checklist": bool(steps), "step_choices": STEP_CHOICES, "reading_max": completion.READING_MAX, **(offers or {}),
        **_follow_up(wo),
        "requester_emailed": _requester_email(wo),
        # Slice 24: an open PM is started as it is completed; the Hours box; where the modal was opened from
        "starting": not reason and wo.status == WoStatus.OPEN, **(hours or {"offer_hours": False}),
        "from_my_work": from_my_work(request), "from_value": FROM_MY_WORK,
        **(late if late is not None and form is not None else {"late_offer": None}),  # slice 25: "Why was it late?"
        # Slice 31: who did the vendor's work, as the form holds it
        "engineer": (views_wo_engineer.picked((offers or {})["engineer_select"], form, "wc")
                     if form is not None and (offers or {}).get("engineer_select") is not None and "vendor_engineer" in form.fields else None),
        # Slice 33: what the repair found, as the form holds it
        "finding": (views_wo_finding.picked((offers or {})["finding_select"], form, "wc")
                    if form is not None and (offers or {}).get("finding_select") is not None and "repair_finding" in form.fields else None),
        # Slice 35: the test equipment used, as the form holds it (judged today)
        "te": (views_wo_test_equipment.picked((offers or {})["te_picker"], form, prefix="wc",
                                              vendor_report=(offers or {}).get("offer_vendor_report", False))
               if form is not None and (offers or {}).get("te_picker") is not None and "test_equipment" in form.fields else None),
        # Slice 38: what the software update did, as the form holds it
        "release": views_wo_release.picked((offers or {}).get("release_picker"), form),
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
    if wo.type == WoType.REPAIR and wo.repair_check:  # slice 30: "Checked after repair; CE-10241 back in service"
        note = completion.repair_note(wo, done)
        if wo.repair_check == RepairCheck.NOT_RETURNED and done.tagged_out:  # it was in use: say what moved
            note = f"{wo.get_repair_check_display()}: {wo.asset.tag} tagged out of service"
        return f"{wo.number} completed: {note}{logged}"
    if wo.type == WoType.SOFTWARE_UPDATE:  # slice 38: what it recorded, for a user who may see it ("" when it recorded nothing)
        words = views_wo_release.toast_words(wo, done, request.user)
        return f"{wo.number} completed{f'; {words}' if words else ''}{logged}"
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
    inspection (slice 26), its re-inspection. `late_given`: a reason for lateness came with it (slice 25), which the toast names.
    Slice 38: a completion that recorded what a software update did (done.software) changed the device's recorded release, so the
    Device security screen's lists (opened behind the drawer) refetch themselves on CYBER_CHANGED, as every other register write's do."""
    if from_my_work(request) and wo.pm_result != PmResult.FAIL and wo.inspection_result != InspectionResult.FAILED:
        response = reswap(HttpResponse(), "none")
    else:
        response = retarget(_render_wo_drawer(request, wo), "#drawer")
    events = ("wo-changed", *(("devices-changed",) if done.device_changed else ()),
              *((CYBER_CHANGED,) if done.software is not None else ()))
    for event in events:
        trigger_client_event(response, event, {})
    toast(response, _message(request, wo, done, late_given))
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _kept_message(wo, kept) -> str:
    """keep_open's toast (slice 30): the repair stays open with its failed checks, and what the hold did to the device."""
    tag = wo.asset.tag
    message = f"{wo.number} kept open: failed checks recorded"
    if kept.tagged_out:
        return f"{message}; {tag} tagged out of service"
    if kept.held:
        return f"{message}; {tag} held out of use until it is checked"
    return message


def _kept(request, wo, kept):
    """A repair kept open after a failed check (slice 30): answered as a save is (_saved), the drawer with the note in its timeline, or
    from My work nothing swapped; devices-changed when the device went out of service."""
    response = reswap(HttpResponse(), "none") if from_my_work(request) else retarget(_render_wo_drawer(request, wo), "#drawer")
    for event in ("wo-changed", *(("devices-changed",) if kept.tagged_out else ())):
        trigger_client_event(response, event, {})
    toast(response, _kept_message(wo, kept))
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _setup(request, wo, today) -> tuple:
    """The modal's parts for `wo` today: its checklist's steps, the offers, the hours box, the late reason, and the form's kwargs."""
    steps = completion.checklist_of(completion.procedure_for(wo))
    offers = {**_offers(wo, request.user, today), **_vendor_offers(wo, request.user, today),  # slice 31: who did the vendor's work
              "te_picker": views_wo_test_equipment.offer(wo, request.user, today),  # slice 35: None where it records none
              "release_picker": views_wo_release.offer(wo, request.user, today)}  # slice 38: None where nothing is offered
    hours = _hours(request, wo)
    late = _late(request, wo, today)  # slice 25
    return steps, offers, hours, late, _form_kwargs(wo, steps, offers, hours, late)


def _refused(request, number, form, today, steps, offers, hours, late, kwargs):
    """The modal again after a refusal, with what was typed and the errors where they belong. The work order as it is now: a refusal for
    its state (completed meanwhile) re-renders as the blocker. A checklist revised while the modal was open shows its steps anew
    (_revised); any other checklist error keeps the steps as they were typed (slice 30: "No check failed" after Keep open)."""
    wo = get_wo(request, number)
    reason = completion.blocker(wo, today, request.user)
    if not reason and any(m in REVISIONS for m in form.errors.get("checklist", [])):
        form = _revised(form, kwargs)
    return _modal(request, wo, None if reason else form, steps=steps, offers=offers, hours=hours, late=late, reason=reason)


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
    steps, offers, hours, late, kwargs = _setup(request, wo, today)
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
        return _refused(request, number, form, today, steps, offers, hours, late, kwargs)
    return _saved(request, wo, done, late_given=bool(form.offer_late_reason and form.cleaned_data.get("late_reason")))  # the service refreshed wo


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL, scoped=True)
def wo_keep_open(request, number):
    """"If a check fails" (slice 30): the completion modal's Keep the repair open posts here with the steps as typed. The failed checks
    are recorded on the repair, which stays open, and with the hold box the device is held out (completion.keep_open). Work orders Edit,
    as completing (wo_perms.can_keep_open); a repair only (another type is a 404); a refusal re-renders the modal as completing does."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    _require(wo_perms.can_keep_open(request.user))
    wo = get_wo(request, number)
    if wo.type != WoType.REPAIR:
        raise Http404
    today = timezone.localdate()
    reason = completion.blocker(wo, today, request.user)
    if reason:
        return _modal(request, wo, reason=reason)
    steps, offers, hours, late, kwargs = _setup(request, wo, today)
    form = CompleteForm(request.POST, **kwargs)
    form.check_engineer = False  # slice 31: keeping it open names no engineer, so an unfinished "Someone else" never stops it
    kept = None
    if form.is_valid():
        try:
            kept = completion.keep_open(wo, by=request.user, today=today, **form.keep_open_kwargs())
        except ValidationError as e:
            form.add_service_errors(e)
    if kept is None:
        return _refused(request, number, form, today, steps, offers, hours, late, kwargs)
    return _kept(request, wo, kept)  # the service refreshed wo
