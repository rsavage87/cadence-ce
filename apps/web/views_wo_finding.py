"""
What a repair found (slice 33, apps.workorders.findings): the select the completion modal and the drawer's Change share, the drawer's
"What was found" row, and Change itself. Views parse input and call the services (completion.complete_work_order and
workorders.services.set_repair_finding, the two writers of WorkOrder.repair_finding); the rule is apps.workorders.findings.

The select (web/_wo_finding_select.html), on a repair: "What was found", one native select, so it needs no round trip on a phone: a
blank "Choose what was found" first (nothing is preselected on a repair with none stored), then findings.GROUPS as optgroups, each
limited to findings.choices_for(the repair, the user) (labels of at most 30 characters, so a phone's picker never cuts them). Under it
the chosen one's hint (findings.hint, worded for the choices offered: CSS :has shows the one chosen; without :has support the one the
page was drawn with), and a "What each means" disclosure with every offered choice's hint and findings.order_words, the order to decide
by among them. A failed PM's repair
says "Found on PM <number>: choose what failed." (the PM's number only when the user may see it); an incident's investigation that use
error is not offered on it (never the incident's number). For a user who sees the whole facility, the earlier service on the device
before the repair was opened (`evidence`: its last PM, and any other repair or PM done in the EVIDENCE_DAYS before), the evidence for
"PM could have caught it" and "Caused by earlier service", which are the facility's to choose.

The finding is the facility's record, never shown to a scoped user (a vendor's or a requester's account): the stored finding is
prefilled only for a user who sees the whole facility (a reopened repair completed again). A scoped user's select starts at "Keep what
was recorded" when one is stored that they may choose (their own observations: they may replace it), never naming it; when the stored
finding is one they cannot choose (use error, PM could have caught it, caused by earlier service, not stated: the facility's), there is
no select, only "Recorded by the facility: it stays as recorded." (completion keeps it when nothing is sent; findings.FACILITY_RECORDED
refuses a change from their account). Both only while the repair's own limits allow the finding stored (findings.choices_for with no
user, as completion keeps it): one they refuse now is chosen again, by anyone, so the select is offered blank.

The drawer's row (drawer_context, under "what_found", through views_wo_complete.results_context), on a completed or closed repair, for
a user who sees the whole facility only: the finding's label, or "Not recorded" (completed before Cadence recorded findings, or
imported), whether it counts as a device failure (findings.failure_q's rule for a completed repair), and "Change" ("Record" when none
is recorded) when workorders.permissions.can_set_repair_finding (Work orders Edit, Approve once closed, never a scoped user).

Change (wo_finding, work-orders/<number>/finding/): a modal with the select, posting to services.set_repair_finding. Never a scoped
user's (web_view refuses them). A work order that is not a repair, is open or cancelled, or is closed for a user without Approve, gets
the modal saying why. A refusal (keyed repair_finding, in findings' words) re-renders it with what was chosen; a save answers with the
drawer, a toast, wo-changed, and the modal's close after settle. A direct visit opens the work order.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django_htmx.http import retarget, trigger_client_event

from apps.workorders import completion, findings, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import RepairFinding, Source, WorkOrder, WoStatus, WoType

from .decorators import web_view
from .forms_wo_complete import FindingForm
from .htmx import toast
from .views import _render_wo_drawer, get_wo

MODAL = "web/_wo_finding.html"
CHOOSE = "Choose what was found"
KEEP_RECORDED = "Keep what was recorded"  # a scoped user's blank, when a finding they may choose is stored: never named to them
FACILITY_KEPT = "Recorded by the facility: it stays as recorded."
FOUND_ON_PM = "Found on PM {number}: choose what failed."
FOUND_ON_A_PM = "Found on a PM: choose what failed."  # the PM is outside a scoped user's share
INVESTIGATION = "An incident's investigation: use error is not offered (who erred stays in the facility's event report)."
EVIDENCE_DAYS = 90  # earlier service this close before the repair was opened is the evidence for "Caused by earlier service"
EVIDENCE_MAX = 5  # the most recent ones named
NOT_RECORDED = "Not recorded"
NOT_RECORDED_WHY = "Completed before Cadence recorded what repairs found"
IMPORTED_WHY = "Imported from the previous system"
FAILURE = "Counted as a device failure"
NOT_FAILURE = "Not a device failure: never counted as one"
UNKNOWN_FAILURE = "Counted as a device failure until what was found is recorded"
NO_CAUSE = "Counted as a device failure: no cause stated"
CLOSED = "{number} is closed: recording what was found now needs Work orders Approve."
CANCELLED = "{number} was cancelled: it records no finding."
NOT_DONE = "{number} is not completed yet: record what was found when completing it."


# --- the select ----------------------------------------------------------------------------------------------------------------

def _failed_pm(wo):
    """The failed PM `wo` was opened from (completion's follow-up repair), or None: findings.found_on_pm's rule, with the PM's number."""
    if not wo.follow_up_of_id:
        return None
    return WorkOrder.objects.filter(pk=wo.follow_up_of_id, type=WoType.PM).only("id", "number", "asset_id").first()


def evidence(wo) -> dict:
    """The earlier service on `wo`'s device before it was opened (two queries): its last PM done on or before that day (a PM work
    order, else the device's own last PM date, as an import records it), and the other repairs and PMs done in the EVIDENCE_DAYS
    before (the most recent EVIDENCE_MAX, the last PM apart). For a user who sees the whole facility: the numbers are the facility's."""
    asset, day = wo.asset, wo.opened_on
    done = WorkOrder.objects.filter(asset_id=asset.pk, status__in=completion.DONE_STATUSES, completed_on__lte=day).exclude(pk=wo.pk)
    last_pm = done.filter(type=WoType.PM).order_by("-completed_on", "-number").only("number", "completed_on").first()
    last_pm_on = last_pm.completed_on if last_pm is not None else (asset.last_pm_on if asset.last_pm_on and asset.last_pm_on <= day else None)
    since = day - timedelta(days=EVIDENCE_DAYS)
    recent = done.filter(type__in=(WoType.REPAIR, WoType.PM), completed_on__gte=since)
    if last_pm is not None:
        recent = recent.exclude(pk=last_pm.pk)
    recent = [{"kind": "PM" if w.type == WoType.PM else "repair", "number": w.number, "completed_on": w.completed_on}
              for w in recent.order_by("-completed_on", "-number").only("number", "type", "completed_on")[:EVIDENCE_MAX]]
    return {"day": day, "tag": asset.tag, "last_pm": last_pm, "last_pm_on": last_pm_on, "recent": recent, "days": EVIDENCE_DAYS,
            "since": since}


def select_context(wo, user) -> dict:
    """The select's options for repair `wo` as `user` may choose them (findings.choices_for, read once): {"groups": [{"label",
    "options": [{"value", "label", "hint"}]}], "initial": the stored finding for a user who sees the whole facility (when still offered),
    "keep": KEEP_RECORDED for a scoped user with one stored they may choose, "locked": a scoped user's stored finding they cannot choose
    (no select), "from_pm": a failed PM's repair's words, "investigation": the investigation's words (a user who sees the whole facility),
    "evidence": the earlier service (the same), "means": every offered choice with its hint (findings.hint), "order": the order to
    decide by among the offered choices (findings.order_words)}."""
    scoped = scoping.is_scoped(user)
    pm = _failed_pm(wo)
    investigation = findings.is_investigation(wo)
    choices = findings.choices_for(wo, user, investigation=investigation, from_pm=pm is not None)
    stored = wo.repair_finding
    # Kept as stored only while the repair's own limits allow it (completion's rule): one they refuse now (a stored "PM could have
    # caught it" on a repair a PM's failure was recorded on since) is chosen again, a scoped user's too
    kept = bool(stored) and stored in findings.choices_for(wo, None, investigation=investigation, from_pm=pm is not None)
    groups = []
    for label, values in findings.GROUPS:
        options = [{"value": str(v), "label": RepairFinding(v).label, "hint": findings.hint(v, choices)} for v in values if v in choices]
        if options:
            groups.append({"label": label, "options": options})
    from_pm = ""
    if pm is not None:
        from_pm = FOUND_ON_PM.format(number=pm.number) if scoping.can_see_work_order(user, pm) else FOUND_ON_A_PM
    return {"groups": groups, "choices": choices, "initial": "" if scoped or stored not in choices else stored,
            "keep": KEEP_RECORDED if scoped and kept and stored in choices else "",
            "locked": findings.facility_holds(wo, user, investigation=investigation, from_pm=pm is not None),  # completion's rule
            "kept_words": FACILITY_KEPT,
            "from_pm": from_pm, "investigation": INVESTIGATION if investigation and not scoped else "",
            "evidence": None if scoped else evidence(wo),
            "means": [o for g in groups for o in g["options"]], "order": findings.order_words(choices), "choose": CHOOSE}


def picked(sel: dict, form, prefix: str) -> dict:
    """The select as `form` holds it: the value chosen (one not offered is shown as the blank choice; its refusal says why), its error,
    and whether to focus it (the completion modal's first error; Change's)."""
    raw = form["repair_finding"].value() if "repair_finding" in form.fields else ""
    chosen = "" if raw is None else str(raw).strip()
    if chosen not in sel["choices"]:
        chosen = ""
    error = " ".join(form.errors.get("repair_finding", [])) if form.is_bound else ""
    return {**sel, "value": chosen, "error": error, "focus": getattr(form, "first_error", "") == "repair_finding", "prefix": prefix}


# --- the drawer's row ----------------------------------------------------------------------------------------------------------

def counting_words(value: str) -> str:
    """Whether a completed repair with this finding counts as a device failure (findings.failure_q's rule), in words."""
    if not value:
        return UNKNOWN_FAILURE
    if value == RepairFinding.NOT_STATED:
        return NO_CAUSE
    return FAILURE if value in findings.RELIABILITY else NOT_FAILURE


def drawer_context(request, wo) -> dict:
    """The drawer's "What was found" row, under "what_found": None unless `wo` is a completed or closed repair and the user sees the
    whole facility (the finding is the facility's record)."""
    if wo.type != WoType.REPAIR or wo.status not in completion.DONE_STATUSES or scoping.is_scoped(request.user):
        return {"what_found": None}
    value = wo.repair_finding
    return {"what_found": {
        "value": value, "label": findings.label(value) if value else NOT_RECORDED,
        "why": "" if value else IMPORTED_WHY if wo.source == Source.IMPORTED else NOT_RECORDED_WHY,
        "counting": counting_words(value),
        "can_change": wo_perms.can_set_repair_finding(request.user, wo),
    }}


# --- Change ---------------------------------------------------------------------------------------------------------------------

def _blocker(user, wo) -> str:
    """Why `wo`'s finding cannot be recorded or changed here now (the service's own words for the work order's state), or ""."""
    if wo.type != WoType.REPAIR:
        return findings.NOT_A_REPAIR
    if wo.status == WoStatus.CANCELLED:
        return CANCELLED.format(number=wo.number)
    if wo.status not in completion.DONE_STATUSES:
        return NOT_DONE.format(number=wo.number)
    if not wo_perms.can_set_repair_finding(user, wo):  # Edit got the user here; a closed repair needs Approve
        return CLOSED.format(number=wo.number)
    return ""


def _modal(request, wo, form=None, blocker=""):
    ctx = {"wo": wo, "asset": wo.asset, "dm": wo.asset.device_model, "blocker": blocker}
    if form is not None and not blocker:
        sel = select_context(wo, request.user)
        if not form.is_bound:
            form.initial["repair_finding"] = sel["initial"]
        form.first_error = "repair_finding" if form.is_bound and "repair_finding" in form.errors else ""
        ctx.update(form=form, finding=picked(sel, form, "wf"), recorded=bool(wo.repair_finding))
    return render(request, MODAL, ctx)


def _saved(request, wo):
    message = f"What was found on {wo.number}: {findings.label(wo.repair_finding)}"
    response = toast(trigger_client_event(retarget(_render_wo_drawer(request, wo), "#drawer"), "wo-changed", {}), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(wo_perms.MODULE, wo_perms.LATE_REASON_LEVEL)  # Work orders Edit (Approve once closed: _blocker); never a scoped user
def wo_finding(request, number):
    wo = get_wo(request, number)  # another facility's number is a 404
    if request.method != "POST" and not request.htmx:  # only ever a modal: a direct visit opens the work order
        return redirect("web:wo", number=wo.number)
    blocker = _blocker(request.user, wo)
    if blocker:
        return _modal(request, wo, blocker=blocker)
    if request.method != "POST":
        return _modal(request, wo, FindingForm())
    form = FindingForm(request.POST)
    if form.is_valid():
        try:
            wo_services.set_repair_finding(wo, form.cleaned_data["repair_finding"], by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
        else:
            return _saved(request, get_wo(request, number))
    wo = get_wo(request, number)  # as it is now: reopened, cancelled, or closed meanwhile is the blocker
    return _modal(request, wo, form, blocker=_blocker(request.user, wo))
