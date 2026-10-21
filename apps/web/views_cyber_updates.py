"""
Software updates on the device drawer's Security tab and the Device security screen's model panel (slice 38; the rules are
apps.cybersecurity.rules, every write apps.cybersecurity.updates): Open software update (one device), Record update (an install with no
work order), Confirm version (a completed update work order with nothing recorded), Entered in error, and Open updates to this release
(the batch, from a release row of the model panel). Views parse input (forms_cyber_updates.py), call the service, and answer with the
drawer or the panel and a toast in rules' words; the levels are checked here and again inside the services. Never a scoped user's
(web_view).

- GET opens the modal (into #modal-card): the form, or why it cannot be done now (a blocker in the services' words: a device not on
  site, held as evidence, with an open update already; an update not completed with the version not confirmed; nothing recorded).
  Opened directly (no HTMX), a device's door shows the device's drawer on its Security tab, the batch the model's panel.
- POST: the form's values as typed to the service. A refusal comes back under the field it names (one about the device, the update, or
  the release at the top), with what was chosen kept. Saved: the device's drawer on the Security tab (the batch: the model's panel with
  its filters, which the modal's address carries), a toast in rules' words (rules.update_words of the row: "; a later record of the
  device is kept" when the device's facts were newer; the batch's counts, why its work orders were left unassigned, and each skip with
  its tags), "cyber-changed" and "wo-changed" for the lists showing either, and the modal's close after settle.

Who: Open software update and the batch: permissions.can_open_updates (Device security Edit, Equipment View, Work orders Edit); Record
update and Confirm version: can_edit_devices; Entered in error: can_correct_updates (Device security Approve). Another facility's
device, update, or release (or a hand-recorded update entered in error, or the model's profile for the batch) is a 404.
"""
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.credentials.services import qualified_technicians
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules, updates
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import SoftwareRelease, SoftwareUpdate
from apps.equipment.models import Department
from apps.equipment.services import owner_maintains, service_vendor
from apps.workorders.models import OPEN_STATUSES

from . import asset_cyber, views_cybersecurity
from .decorators import web_view
from .forms_cyber_updates import ConfirmForm, OpenUpdateForm, OpenUpdatesForm, RecordUpdateForm, RemoveForm, batch_routing, routing
from .htmx import toast
from .views import asset_drawer_context, get_asset

MODULE = Module.CYBERSECURITY
DRAWER = "web/_asset_drawer.html"
OPEN = "web/_cyber_update_open.html"
RECORD = "web/_cyber_update_record.html"
CONFIRM = "web/_cyber_update_confirm.html"
REMOVE = "web/_cyber_update_remove.html"
BATCH = "web/_cyber_open_updates.html"
CHANGED = "cyber-changed"  # the register changed (views_cybersecurity.CHANGED): the screen's lists refetch themselves
WO_CHANGED = "wo-changed"  # an update's work order opened or changed: the work order lists refetch themselves

# The toasts' frames (the record in rules' words inside them)
OPENED = "{number} opened for {tag}. {words}."
RECORDED = "Update recorded for {tag}: {words}"
CONFIRMED = "{number} confirmed: {words}"
OPENED_TO_INSTALL = "Opened to install {version}."
OPENED_TO_PATCH = "Opened to apply the manufacturer's patches{ref}."
FACTS_STAY = "The device's facts stay as they were: correct its release with Record facts if it should change."
HAND_REMOVED = "The update recorded by hand on {tag} is marked entered in error. " + FACTS_STAY
OUTCOME_REMOVED = "{number}'s outcome is marked entered in error: Confirm version is offered again. " + FACTS_STAY
BATCH_OPENED = "{count} opened to install {version}."
NONE_OPENED = "No software update opened."
LEFT_UNASSIGNED = "{count} left unassigned. {reason}"
SKIPS = (("held", "Skipped, held as evidence for an incident: {tags}."),
         ("already_open", "Skipped, with an open software update already: {tags}."),
         ("changed", "Skipped, its model changed or it was retired meanwhile: {tags}."))
# The batch's modal when there is nothing to open
NO_DEVICES = "No device of this model is on site."
ALL_ON_IT = "Every device of this model on site already runs {version}: there is nothing to open."
ALREADY_ON_IT = "{on_site} of this model on site; {count} already {verb} {version}."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- lookups ------------------------------------------------------------------------------------------------------------------------

def _update(request, pk):
    """An update of this facility with its work order, releases, and device read (rules.update_words reads them): another facility's,
    an unknown id, or an install recorded by hand that was entered in error (hidden from the tab, kept in history) is a 404; so is one
    on a device this user may not see. Returns (update, its device as the drawer reads it)."""
    found = (SoftwareUpdate.objects.select_related("asset", "work_order", "target_release", "release_before", "release_after")
             .filter(pk=pk).first())
    if found is None or (found.work_order_id is None and found.removed_on is not None):
        raise Http404
    return found, get_asset(request, found.asset.tag)


def _target(pk) -> SoftwareRelease:
    """The release to install for the batch: a versioned release of this facility not entered in error (another facility's, the
    model's profile, or one entered in error is a 404)."""
    found = (SoftwareRelease.objects.select_related("device_model").filter(pk=pk, removed_on__isnull=True).exclude(version_key="")
             .first())
    if found is None:
        raise Http404
    return found


def _drawer_url(asset) -> str:
    return reverse("web:asset", args=[asset.tag]) + f"?tab={asset_cyber.TAB}"


def _open_number(asset) -> str:
    """The number of the device's open software update (open, in progress, or awaiting parts), the lowest, or ""."""
    return (SoftwareUpdate.objects.filter(asset_id=asset.pk, work_order__status__in=OPEN_STATUSES).order_by("work_order__number")
            .values_list("work_order__number", flat=True).first() or "")


# --- answers ------------------------------------------------------------------------------------------------------------------------

def _changed(response):
    trigger_client_event(response, CHANGED, {})
    return trigger_client_event(response, WO_CHANGED, {})


def _saved(request, asset, message: str):
    """Saved: the device's drawer on its Security tab, freshly read, the toast and the events, then the modal closes (after settle:
    closing it first would detach the form that sent this request and cancel the swap)."""
    request.GET = request.GET.copy()
    request.GET["tab"] = asset_cyber.TAB
    response = retarget(render(request, DRAWER, asset_drawer_context(request, get_asset(request, asset.tag))), "#drawer")
    toast(_changed(response), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _panel_saved(request, dm, message: str):
    """The batch saved: the model's panel into #drawer (the filters it was opened with are in this request's address), the toast and
    the events, then the modal closes after settle."""
    response = retarget(render(request, views_cybersecurity.PANEL, views_cybersecurity._panel_context(request, dm)), "#drawer")
    toast(_changed(response), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _modal(request, template: str, ctx: dict):
    form = ctx.get("form")
    if form is not None and form.is_bound:
        form.focus_first_error()
    return render(request, template, ctx)


def _fresh(**lookup) -> SoftwareUpdate:
    """The row as stored now (by pk, or by its work order), with what rules.update_words reads."""
    return SoftwareUpdate.objects.select_related("work_order", "target_release", "release_before", "release_after").get(**lookup)


def _with_later(words: str, row) -> str:
    """The sentence, with "a later record of the device is kept" when the device's facts were newer than the record (update_words says
    it on a done row; never twice)."""
    if row.later_kept and rules.LATER_KEPT_WORDS not in words:
        return f"{words}; {rules.LATER_KEPT_WORDS}"
    return words


# --- Open software update -------------------------------------------------------------------------------------------------------------

def _open_blocker(asset) -> str:
    """Why an update cannot be opened on the device now (the service's words), or ""."""
    gone = cyber._gone(asset)
    if gone:
        return gone
    if asset.incident_hold:
        return updates.HELD.format(tag=asset.tag)
    number = _open_number(asset)
    return updates.ALREADY_OPEN.format(tag=asset.tag, number=number) if number else ""


def _open_form(request, asset, today, data=None) -> tuple[OpenUpdateForm, str]:
    """The form, with Assign to where open_update would use it, and the line saying where the update goes otherwise."""
    user = request.user
    profile = rules.profile(asset.device_model_id)
    kind = asset.get_ownership_display().lower() if owner_maintains(asset) else ""
    assign, route = routing(user, profile.updates_by if profile is not None else "", vendor=service_vendor(asset), temporary_kind=kind)
    dm = asset.device_model
    form = OpenUpdateForm(data, releases=rules.releases(asset.device_model_id), recorded=rules.release_of(asset),
                          model_name=f"{dm.manufacturer} {dm.model}", today=today, assign=assign,
                          qualified=qualified_technicians(asset, today) if assign else ())
    return form, route


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_open(request, tag):
    """Open software update for the device `tag` (GET the modal, POST it): permissions.can_open_updates; updates.open_update."""
    if not cyber_perms.can_open_updates(request.user):
        raise PermissionDenied(updates.OPEN_PERMISSION)
    asset = get_asset(request, tag)
    if request.method != "POST" and not request.htmx:
        return redirect(_drawer_url(asset))
    today = timezone.localdate()
    if request.method != "POST":
        blocker = _open_blocker(asset)
        form, route = _open_form(request, asset, today) if not blocker else (None, "")
        return _modal(request, OPEN, {"asset": asset, "form": form, "route": route, "blocker": blocker})
    form, route = _open_form(request, asset, today, request.POST)
    batch = None
    if form.is_valid():
        try:
            batch = updates.open_update(asset, by=request.user, today=today, **form.service_kwargs())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if batch is None:
        return _modal(request, OPEN, {"asset": asset, "form": form, "route": route, "blocker": ""})
    wo = batch.work_orders[0]
    row = _fresh(work_order_id=wo.pk)
    message = OPENED.format(number=wo.number, tag=asset.tag, words=rules.update_words(row))
    if batch.unassigned_reason:
        message += f" {batch.unassigned_reason}"
    return _saved(request, asset, message)


# --- Record update --------------------------------------------------------------------------------------------------------------------

def _record_form(asset, today, data=None) -> RecordUpdateForm:
    dm = asset.device_model
    return RecordUpdateForm(data, releases=rules.releases(asset.device_model_id), recorded=rules.release_of(asset),
                            model_name=f"{dm.manufacturer} {dm.model}", today=today)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_record(request, tag):
    """Record update for the device `tag`, an install with no work order: permissions.can_edit_devices; updates.record."""
    if not cyber_perms.can_edit_devices(request.user):
        raise PermissionDenied(updates.RECORD_PERMISSION)
    asset = get_asset(request, tag)
    if request.method != "POST" and not request.htmx:
        return redirect(_drawer_url(asset))
    today = timezone.localdate()
    if request.method != "POST":
        blocker = cyber._gone(asset)
        return _modal(request, RECORD, {"asset": asset, "form": None if blocker else _record_form(asset, today), "blocker": blocker})
    form = _record_form(asset, today, request.POST)
    row = None
    if form.is_valid():
        try:
            row = updates.record(asset, form.choice(), by=request.user, today=today, **form.service_kwargs())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if row is None:
        return _modal(request, RECORD, {"asset": asset, "form": form, "blocker": ""})
    return _saved(request, asset, RECORDED.format(tag=asset.tag, words=_with_later(rules.update_words(_fresh(pk=row.pk)), row)))


# --- Confirm version ------------------------------------------------------------------------------------------------------------------

def _confirm_blocker(update, asset) -> tuple[object, str]:
    """(the offer, or None; why Confirm version cannot be done now, or "") in the services' words: an install recorded by hand, or a
    work order not completed with the version not confirmed (reopened: updates.REOPENED; recorded meanwhile; cancelled), or a device
    not on site (rules.offer_or_why's NOT_ON_SITE)."""
    if update.work_order_id is None:
        return None, updates.CONFIRM_WHAT
    state = rules.update_state(update)
    if state == rules.OPEN:
        return None, updates.REOPENED.format(number=update.work_order.number)
    if state == rules.DONE:
        return None, updates.RECORDED_MEANWHILE
    if state != rules.NOT_CONFIRMED:
        return None, updates.CONFIRM_WHAT
    offer, why = rules.offer_or_why(update.work_order, None)  # the record and the device (the user's level is checked by the view)
    if offer is None:
        return None, cyber._gone(asset) if why == rules.NOT_ON_SITE else updates.CONFIRM_WHAT
    return offer, ""


def _target_words(update) -> str:
    """What the work order was opened to install (its record's target, never shown outside can_view_devices doors)."""
    if update.target_release_id:
        return OPENED_TO_INSTALL.format(version=rules.release_name(update.target_release))
    if update.work_order_id:
        return OPENED_TO_PATCH.format(ref=f" (ref {update.target_reference})" if update.target_reference else "")
    return ""


def _confirm_form(update, asset, offer, data=None) -> ConfirmForm:
    dm = asset.device_model
    releases = offer.releases if offer is not None else tuple(rules.releases(asset.device_model_id))
    recorded = offer.recorded if offer is not None else rules.release_of(asset)
    target = update.target_release if update.target_release_id else None
    return ConfirmForm(data, releases=releases, recorded=recorded, target_release=target, model_name=f"{dm.manufacturer} {dm.model}")


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_confirm(request, pk):
    """Confirm version on the update `pk` (a completed or closed work order's, nothing recorded): permissions.can_edit_devices;
    updates.confirm."""
    if not cyber_perms.can_edit_devices(request.user):
        raise PermissionDenied(updates.RECORD_PERMISSION)
    update, asset = _update(request, pk)
    if request.method != "POST" and not request.htmx:
        return redirect(_drawer_url(asset))
    offer, blocker = _confirm_blocker(update, asset)
    ctx = {"asset": asset, "update": update, "number": update.work_order.number if update.work_order_id else "",
           "words": rules.update_words(update), "target": _target_words(update)}
    if request.method != "POST":
        return _modal(request, CONFIRM, {**ctx, "blocker": blocker, "form": None if blocker else _confirm_form(update, asset, offer)})
    form = _confirm_form(update, asset, offer, request.POST)
    row = None
    if form.is_valid():
        try:
            row = updates.confirm(update, form.choice(), by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if row is None:
        return _modal(request, CONFIRM, {**ctx, "blocker": "", "form": form})
    words = _with_later(rules.update_words(_fresh(pk=row.pk)), row)
    return _saved(request, asset, CONFIRMED.format(number=update.work_order.number, words=words))


# --- Entered in error -----------------------------------------------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.APPROVE)
def update_remove(request, pk):
    """Entered in error on the update `pk`: permissions.can_correct_updates; updates.remove. GET the modal saying what it does (or why
    it cannot: nothing recorded); POST marks it. The device's facts are never touched (the toast says to correct them with Record
    facts)."""
    if not cyber_perms.can_correct_updates(request.user):
        raise PermissionDenied(updates.CORRECT_PERMISSION)
    update, asset = _update(request, pk)
    if request.method != "POST" and not request.htmx:
        return redirect(_drawer_url(asset))
    ctx = {"asset": asset, "update": update, "hand": update.work_order_id is None,
           "number": update.work_order.number if update.work_order_id else "", "words": rules.update_words(update)}
    if request.method != "POST":
        return render(request, REMOVE, {**ctx, "form": RemoveForm(), "blocker": "" if update.outcome else updates.NOTHING_RECORDED})
    form = RemoveForm(request.POST)
    form.is_valid()
    try:
        updates.remove(update, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        form.add_service_errors(e)
        return render(request, REMOVE, {**ctx, "form": form, "blocker": ""})
    if ctx["hand"]:
        return _saved(request, asset, HAND_REMOVED.format(tag=asset.tag))
    return _saved(request, asset, OUTCOME_REMOVED.format(number=ctx["number"]))


# --- Open updates to this release (the batch) -----------------------------------------------------------------------------------------

def _batch_context(request, release, today, data=None) -> dict:
    """The batch's modal: the model's other releases with their devices on site (rules.standings: a fixed number of queries), the
    devices with none recorded, those already on the release, the departments with devices of the model on site, and Assign to and the
    routing lines (forms_cyber_updates.batch_routing)."""
    user = request.user
    dm = release.device_model
    on = rules.on_site().filter(device_model=dm)
    read = rules.standings(on, today)
    running: dict = {}
    for s in read.values():
        key = s.release.pk if s.release is not None else None
        running[key] = running.get(key, 0) + 1
    others = [(r, running.get(r.pk, 0)) for r in rules.releases(dm) if r.pk != release.pk]
    departments = Department.objects.filter(pk__in=on.values("department_id")).order_by("name").values_list("pk", "name")
    temporary = sum(1 for s in read.values() if owner_maintains(s.asset))
    profile = rules.profile(dm)
    assign, lines = batch_routing(user, profile.updates_by if profile is not None else "", temporary=temporary)
    # credentials name a model, its manufacturer, or its category: the model's devices share one answer (qualification reads only that)
    qualified = qualified_technicians(SimpleNamespace(device_model=dm), today) if assign else ()
    form = OpenUpdatesForm(data, others=others, not_recorded=running.get(None, 0), departments=[(str(d), n) for d, n in departments],
                           today=today, assign=assign, qualified=qualified)
    on_it = running.get(release.pk, 0)
    blocker = NO_DEVICES if not read else ALL_ON_IT.format(version=rules.release_name(release)) if on_it == len(read) else ""
    already = (ALREADY_ON_IT.format(on_site=_plural(len(read), "device"), count=on_it, verb="runs" if on_it == 1 else "run",
                                    version=rules.release_name(release)) if on_it and not blocker else "")
    return {"dm": dm, "release": release, "form": form, "routes": lines, "already": already, "blocker": blocker,
            "action": request.get_full_path()}


def _batch_message(release, batch) -> str:
    """The batch's toast: how many opened, how many left unassigned and why, and each skip with its tags."""
    parts = [BATCH_OPENED.format(count=_plural(batch.created, "software update"), version=rules.release_name(release))
             if batch.created else NONE_OPENED]
    if batch.unassigned:
        parts.append(LEFT_UNASSIGNED.format(count=batch.unassigned, reason=batch.unassigned_reason))
    for key, words in SKIPS:
        tags = getattr(batch, key)
        if tags:
            parts.append(words.format(tags=", ".join(tags)))
    return " ".join(parts)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def release_open_updates(request, pk):
    """Open updates to the release `pk` (the batch, from the model panel's release row): permissions.can_open_updates;
    updates.open_updates. Saved, the model's panel comes back with the filters it was opened with (they are in this address)."""
    if not cyber_perms.can_open_updates(request.user):
        raise PermissionDenied(updates.OPEN_PERMISSION)
    release = _target(pk)
    dm = release.device_model
    if request.method != "POST" and not request.htmx:
        query = request.GET.urlencode()
        return redirect(reverse("web:cybersecurity_model", args=[dm.pk]) + (f"?{query}" if query else ""))
    today = timezone.localdate()
    if request.method != "POST":
        return _modal(request, BATCH, _batch_context(request, release, today))
    ctx = _batch_context(request, release, today, request.POST)
    form = ctx["form"]
    batch = None
    if form.is_valid():
        try:
            batch = updates.open_updates(release, by=request.user, today=today, **form.service_kwargs())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if batch is None:
        return _modal(request, BATCH, {**ctx, "blocker": ""})
    return _panel_saved(request, dm, _batch_message(release, batch))

