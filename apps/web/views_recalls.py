"""
Recalls and alerts screen (slice 6): one card per alert match, grouped by pills, with the disposition
buttons and the recall work-order batch. Views parse input, call apps.recalls.services, and render.

Every POST answers with the body (#rc-body) re-rendered for the same ?view= and ?match= the page
showed (card buttons carry them in their URL; the page-head buttons rely on HX-Current-URL), and
toasts. The body also listens for `recalls-changed from:body` so a change made elsewhere on the
page can refresh it; nothing fires it yet. Check FDA feed (slice 15) also sends the page head's
sub line out of band: the newest notice date and the last check can change.
"""
from urllib.parse import urlencode, urlsplit

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import QueryDict
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import Level, Module
from apps.recalls import feeds
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


def _params(request):
    """The list's query (view, match). A POST from the page head carries none, so fall back to the page's own URL (HX-Current-URL)."""
    if request.GET or not request.htmx or not request.htmx.current_url:
        return request.GET
    return QueryDict(urlsplit(request.htmx.current_url).query)


def _actions(level: int, m, n_devices: int, qs: str) -> list[dict]:
    """The footer buttons the mock offers for this status, only those the user's recalls `level` allows (the views enforce the same levels)."""

    def move(to, label, primary=False, ghost=False, confirm=""):
        if level < rc_perms.transition_level(m.status, to):
            return None
        return {"url": reverse("web:recall_status", args=[m.pk]) + qs, "to": to, "label": label, "primary": primary, "ghost": ghost, "confirm": confirm}

    def work_orders():
        if not n_devices or level < rc_perms.WORK_ORDERS_LEVEL:
            return None
        label = f"Create {n_devices} work order{'' if n_devices == 1 else 's'}"
        return {"url": reverse("web:recall_work_orders", args=[m.pk]) + qs, "to": "", "label": label, "primary": True, "ghost": False,
                "confirm": f"{label} for {m.device_model}, due in {rc.RECALL_DUE_DAYS} days?"}

    # Dispositions are reversible (Reopen), so like the mock they act at once; only the work-order batch asks first.
    if m.status == S.NEEDS_ACTION:
        items = [work_orders(), move(S.UNDER_REVIEW, "Mark under review")]
    elif m.status == S.UNDER_REVIEW:
        items = [work_orders(), move(S.NOT_AFFECTED, "Close, no action needed")]
    elif m.status == S.IN_PROGRESS:
        items = [move(S.CLOSED, "Close")]
    else:
        items = [move(S.UNDER_REVIEW, "Reopen", ghost=True)]
    return [b for b in items if b]


def _card(level: int, params, m, expanded, qs: str) -> dict:
    n = m.devices
    is_expanded = expanded is not None and m.pk == expanded
    card = {"match": m, "alert": m.alert, "devices": n, "dept_summary": rc.department_summary(m) if n else "", "done": m.status in rc.DONE_STATUSES,
            "expanded": is_expanded, "actions": _actions(level, m, n, qs), "progress": rc.progress(m) if m.status == S.IN_PROGRESS else None,
            "toggle_url": reverse("web:recalls") + query(params, match=None if is_expanded else str(m.pk)),
            # open=0 then open=1: the Work orders filter form sends a hidden 0 ahead of the checkbox and the last value wins.
            "wo_url": f"{reverse('web:workorders')}?{urlencode([('type', 'recall'), ('q', m.alert.external_id), ('open', '0'), ('open', '1')])}",
            "equipment_url": f"{reverse('web:equipment')}?{urlencode({'q': m.device_model.model})}"}
    if is_expanded:
        card["rows"] = list(m.affected_assets().select_related("device_model", "department").order_by("tag")[:DEVICE_ROWS])
    return card


def _body_context(request) -> dict:
    params = _params(request)
    view = params.get("view") if params.get("view") in rc.VIEW_GROUPS else "all"
    expanded = parse_uuid(params.get("match", ""))
    qs = query(params)
    counts = rc.group_counts()
    level = request.user.level_for(rc_perms.MODULE)  # resolved once for every button on the page
    cards = [_card(level, params, m, expanded, qs) for m in rc.filter_matches(view)]
    return {"nav_active": "recalls", "list_url": reverse("web:recalls"), "params": params, "view": view, "expanded": expanded,
            "pills": [{"key": k, "label": label, "count": counts[k], "active": k == view} for k, label in rc.VIEW_LABELS],
            "cards": cards, "has_sample": any(c["alert"].raw.get("demo") for c in cards if isinstance(c["alert"].raw, dict)),
            "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW)}


def _render_body(request):
    return render(request, "web/_recalls_body.html", _body_context(request))


@web_view(rc_perms.MODULE, Level.VIEW)
def recalls(request):
    if is_partial(request, "rc-body"):
        return _render_body(request)
    ctx = {**_body_context(request), **_feed_context(), "can_match": request.user.has_level(rc_perms.MODULE, rc_perms.MATCH_LEVEL),
           "can_view_reports": request.user.has_level(Module.REPORTS, Level.VIEW)}  # the Response log button prints the recall report
    return render(request, "web/recalls.html", ctx)


def _feed_context() -> dict:
    """The page head's sub line: when the newest FDA notice arrived and when the feed last answered (both global)."""
    return {"feed_at": rc.feed_imported_at(), "checked_at": feeds.last_checked()}


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
        batch = rc.create_recall_work_orders(match, by=request.user)
    except ValidationError as e:
        return toast(_render_body(request), e.messages[0])
    n = batch.created
    if n == 0:
        message = "Every affected device already has a recall work order"
    elif batch.unassigned:
        message = f"{n} recall work order{'' if n == 1 else 's'} created; no credentialed technician, left unassigned"
    else:
        message = f"{n} recall work order{'' if n == 1 else 's'} created and assigned to credentialed technicians"
    return toast(_render_body(request), message)


@require_POST
@web_view(rc_perms.MODULE, rc_perms.MATCH_LEVEL)
def recall_match(request):
    n = rc.rematch()
    return toast(_render_body(request), f"{n} new match{'' if n == 1 else 'es'}" if n else "No new matches")


@require_POST
@web_view(rc_perms.MODULE, rc_perms.MATCH_LEVEL)
def recall_check_feed(request):
    """Check FDA feed: fetch the last 30 days from openFDA (at most once per feeds.CHECK_COOLDOWN, whoever asks), store them, and
    match them to this facility. At the level of Match alerts to inventory, which it extends with the fetch."""
    try:
        message = feeds.check_message(feeds.check_feed())
    except feeds.FeedError as e:
        message = f"Could not check the FDA recall feed: {e}. Nothing changed; the daily import will try again."
    return toast(render(request, "web/_recalls_check.html", {**_body_context(request), **_feed_context()}), message)
