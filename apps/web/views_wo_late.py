"""
Why a PM was late (slice 25, the survey binder): the work order drawer's "Why late" row (web/_wo_late.html), on a PM that missed its
on-time window (apps.pm.services.missed_due_date: open past it, completed after it, or cancelled; slice 27, the window is the due date
unless the facility chose otherwise, apps.pm.windows), and the select in it that saves itself. A PM past its due date but still inside
its window has no row: it is not late yet.

Slice 27: a PM with a reason recorded that is on time now (the facility widened its window since, or its due date moved) keeps the row
("stale"), saying so, and someone who may record reasons can clear it: the select offers only "Not recorded" and the reason on record
(the service sets a reason only on a missed PM, and clears one on any PM).

The row shows the reason recorded (WorkOrder.late_reason, a LateReason) or "Not recorded". Someone who may record it
(permissions.can_set_late_reason: Work orders Edit, Approve once the work order is closed) gets a select instead, which posts on
change to wo_late_reason and is answered with the row re-rendered and a toast; a refusal (the service's words, or a work order closed
since the drawer opened) is shown in the row and nothing is saved. A scoped user (apps.workorders.scoping) sets it only on the work
orders in their share: get_wo is a 404 for any other, as for every move.

A life-support or high-risk PM (its model's class today) with no reason is highlighted: the binder lists it until a reason is
recorded. Not one imported from the previous system (Source.IMPORTED): the binder labels those and never calls them gaps.

The drawer gets the row's context through views_wo_complete.results_context (the PM section the drawer already reads), under "why_late".
"""
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.equipment.models import RiskClass
from apps.pm.services import missed_due_date
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import LateReason, Source, WoType

from .decorators import web_view
from .htmx import toast
from .views import get_wo

ROW = "web/_wo_late.html"
# The risk classes whose late PMs the survey binder lists one by one until a reason is recorded (its maintenance section's gaps)
FLAGGED = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH)
FLAG_NOTE = "The survey binder lists this PM until a reason is recorded."
CLOSED = "{number} is closed: recording why it was late now needs Work orders Approve."
# Slice 27: a reason on a PM that is on time now (the window widened, or the due date moved, since it was recorded)
STALE_NOTE = "On time by the facility's PM policy now, so it needs no reason."
STALE_CLEAR = "Choose Not recorded to clear it."


def flagged_class(wo) -> bool:
    """Whether `wo`'s device is life support or high risk today (the binder reads the model's class as it is now)."""
    return wo.asset.device_model.risk_class in FLAGGED


def late_context(request, wo, *, error: str = "", today=None) -> dict:
    """The "Why late" row, under "why_late": None unless `wo` is a PM that missed its on-time window (two queries, the window and the
    test, only for a PM past its due date), or one on time now with a reason still on record (slice 27: `stale`, offered to clear).
    With `error` and neither (its due date moved since), the row says only why nothing was saved."""
    today = today or timezone.localdate()
    missed = wo.type == WoType.PM and wo.due_on is not None and wo.due_on < today and missed_due_date(wo, today)
    stale = not missed and wo.type == WoType.PM and bool(wo.late_reason)
    if not (missed or stale):
        return {"why_late": {"gone": True, "error": error} if error else None}
    label = wo.get_late_reason_display() if wo.late_reason else ""
    return {"why_late": {
        "reason": wo.late_reason, "label": label,
        "can_set": wo_perms.can_set_late_reason(request.user, wo),
        "choices": [(wo.late_reason, label)] if stale else LateReason.choices,
        "flag": missed and not wo.late_reason and flagged_class(wo) and wo.source != Source.IMPORTED, "flag_note": FLAG_NOTE,
        "imported": wo.source == Source.IMPORTED, "error": error, "stale": stale, "stale_note": STALE_NOTE, "stale_clear": STALE_CLEAR,
    }}


def _row(request, wo, *, error="", today=None):
    return render(request, ROW, {"wo": wo, **late_context(request, wo, error=error, today=today)})


@require_POST
@web_view(wo_perms.MODULE, wo_perms.LATE_REASON_LEVEL, scoped=True)  # Work orders Edit; narrowed to the share by get_wo
def wo_late_reason(request, number):
    """POST late_reason (a LateReason value; blank clears it). Answers with the row (#wo-late) and a toast."""
    wo = get_wo(request, number)  # another facility's, or one outside a scoped user's share, is a 404
    today = timezone.localdate()
    if not wo_perms.can_set_late_reason(request.user, wo):  # Edit got the user here; a closed work order needs Approve
        return _row(request, wo, error=CLOSED.format(number=wo.number), today=today)
    try:
        wo_services.set_late_reason(wo, request.POST.get("late_reason", ""), by=request.user, today=today)
    except ValidationError as e:
        return _row(request, wo, error=" ".join(e.messages), today=today)
    message = (f"Why {wo.number} was late: {wo.get_late_reason_display()}" if wo.late_reason
               else f"Why {wo.number} was late: cleared, not recorded")
    return toast(_row(request, wo, today=today), message)
