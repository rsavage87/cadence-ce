"""
Recalls and alerts screen (slice 6): one card per alert match, grouped by pills, with the disposition
buttons and the recall work-order batch. Views parse input, call apps.recalls.services, and render.

Every POST answers with the body (#rc-body) re-rendered for the same ?view= and ?match= it was
sent from, and toasts. The body also re-fetches itself on `recalls-changed`, which the device
drawer fires; the POSTs here don't, since their response already is the body.
"""
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import Level, Module
from apps.recalls import permissions as rc_perms
from apps.recalls import services as rc
from apps.recalls.models import AlertMatch

from .decorators import web_view
from .forms import parse_uuid
from .htmx import is_partial, toast
from .templatetags.web import query

S = AlertMatch.Status
DEVICE_ROWS = 8  # the mock shows the first eight affected devices, then points at Equipment


def _get_match(pk):
    return get_object_or_404(AlertMatch.objects.select_related("alert", "device_model"), pk=pk)


def _actions(request, m, n_devices: int, qs: str) -> list[dict]:
    """The footer buttons the mock offers for this status, only those the user may press (the views enforce the same levels)."""
    user = request.user

    def move(to, label, primary=False, ghost=False, confirm=""):
        if not rc_perms.can_transition(user, m.status, to):
            return None
        return {"url": reverse("web:recall_status", args=[m.pk]) + qs, "to": to, "label": label, "primary": primary, "ghost": ghost, "confirm": confirm}

    def work_orders():
        if not n_devices or not user.has_level(rc_perms.MODULE, rc_perms.WORK_ORDERS_LEVEL):
            return None
        label = f"Create {n_devices} work order{'' if n_devices == 1 else 's'}"
        return {"url": reverse("web:recall_work_orders", args=[m.pk]) + qs, "to": "", "label": label, "primary": True, "ghost": False,
                "confirm": f"{label} for {m.device_model}, due in {rc.RECALL_DUE_DAYS} days?"}

    label = rc.alert_label(m.alert)
    if m.status == S.NEEDS_ACTION:
        items = [work_orders(), move(S.UNDER_REVIEW, "Mark under review")]
    elif m.status == S.UNDER_REVIEW:
        items = [work_orders(), move(S.NOT_AFFECTED, "Close, no action needed", confirm=f"Close {label} as reviewed, not affected?")]
    elif m.status == S.IN_PROGRESS:
        items = [move(S.CLOSED, "Close", confirm=f"Close {label}? Its work orders stay as they are.")]
    else:
        items = [move(S.UNDER_REVIEW, "Reopen", ghost=True, confirm=f"Reopen {label} for review?")]
    return [b for b in items if b]


def _card(request, m, expanded, qs: str) -> dict:
    n = m.devices
    is_expanded = expanded is not None and m.pk == expanded
    card = {"match": m, "alert": m.alert, "devices": n, "dept_summary": rc.department_summary(m) if n else "", "done": m.status in rc.DONE_STATUSES,
            "expanded": is_expanded, "actions": _actions(request, m, n, qs), "progress": rc.progress(m) if m.status == S.IN_PROGRESS else None,
            "toggle_url": reverse("web:recalls") + query(request.GET, match=None if is_expanded else str(m.pk)),
            # open=0 then open=1: the Work orders filter form sends a hidden 0 ahead of the checkbox and the last value wins.
            "wo_url": f"{reverse('web:workorders')}?{urlencode([('type', 'recall'), ('q', m.alert.external_id), ('open', '0'), ('open', '1')])}",
            "equipment_url": f"{reverse('web:equipment')}?{urlencode({'q': m.device_model.model})}"}
    if is_expanded:
        card["rows"] = list(m.affected_assets().select_related("device_model", "department").order_by("tag")[:DEVICE_ROWS])
    return card


def _body_context(request) -> dict:
    view = request.GET.get("view") if request.GET.get("view") in rc.VIEW_GROUPS else "all"
    expanded = parse_uuid(request.GET.get("match", ""))
    qs = query(request.GET)
    counts = rc.group_counts()
    return {"nav_active": "recalls", "list_url": reverse("web:recalls"), "view": view, "expanded": expanded,
            "pills": [{"key": k, "label": label, "count": counts[k], "active": k == view} for k, label in rc.VIEW_LABELS],
            "cards": [_card(request, m, expanded, qs) for m in rc.filter_matches(view)],
            "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW)}


def _render_body(request):
    return render(request, "web/_recalls_body.html", _body_context(request))


@web_view(rc_perms.MODULE, Level.VIEW)
def recalls(request):
    if is_partial(request, "rc-body"):
        return _render_body(request)
    ctx = {**_body_context(request), "feed_at": rc.feed_imported_at(), "can_match": request.user.has_level(rc_perms.MODULE, rc_perms.MATCH_LEVEL),
           "match_url": reverse("web:recall_match") + query(request.GET)}
    return render(request, "web/recalls.html", ctx)


@require_POST
@web_view(rc_perms.MODULE, rc_perms.REVIEW_LEVEL)
def recall_status(request, pk):
    match = _get_match(pk)
    to_status = request.POST.get("to", "")
    if not rc_perms.can_transition(request.user, match.status, to_status):
        raise PermissionDenied
    try:
        rc.set_status(match, to_status, by=request.user, note=request.POST.get("note", ""))
        message = f"{rc.alert_label(match.alert)}: {match.get_status_display().lower()}"
    except ValidationError as e:
        message = e.messages[0]
    return toast(_render_body(request), message)


@require_POST
@web_view(rc_perms.MODULE, rc_perms.WORK_ORDERS_LEVEL)
def recall_work_orders(request, pk):
    match = _get_match(pk)
    try:
        n = rc.create_recall_work_orders(match, by=request.user)
    except ValidationError as e:
        return toast(_render_body(request), e.messages[0])
    if n == 0:
        message = "Every affected device already has an open recall work order"
    elif rc.unassigned_recall_work_orders(match):
        message = f"{n} recall work order{'' if n == 1 else 's'} created; no credentialed technician, left unassigned"
    else:
        message = f"{n} recall work order{'' if n == 1 else 's'} created and assigned to credentialed technicians"
    return toast(_render_body(request), message)


@require_POST
@web_view(rc_perms.MODULE, rc_perms.MATCH_LEVEL)
def recall_match(request):
    n = rc.rematch()
    return toast(_render_body(request), f"{n} new match{'' if n == 1 else 'es'}" if n else "No new matches")
