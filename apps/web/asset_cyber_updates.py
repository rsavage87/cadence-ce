"""
The device drawer's Security tab, its "Software updates" section (slice 38; web/_asset_cyber_updates.html): the device's update work
orders' records and its installs recorded by hand (rules.updates_of, one query), newest first, each in rules' words (rules.update_state,
rules.update_words); the work order's number always, a link only with Work orders View; Open software update
(permissions.can_open_updates, a device on site and not held as evidence), Record update (can_edit_devices, a device on site), Confirm
version on one completed with nothing recorded (can_edit_devices, a device on site), Entered in error on a done one
(can_correct_updates). No priority chip ("Critical" is a never word). A fixed number of queries however many rows.

The caller (asset_cyber.tab_context) offers the tab only to Device security View with Equipment View (permissions.can_view_devices),
never a scoped user: the versions, references, and targets in the sentences are theirs alone.
"""
from django.urls import reverse

from apps.accounts.models import Level, Module
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules
from apps.equipment.models import AssetStatus

EMPTY = "No software updates recorded yet."
# A state's chip tone (the state's label is rules.STATE_LABELS'): never a priority
TONES = {rules.OPEN: "info", rules.NOT_CONFIRMED: "warn", rules.DONE: "ok", rules.CANCELLED: "neutral", rules.REMOVED: "neutral"}


def open_url(asset) -> str:
    return reverse("web:cybersecurity_update_open", args=[asset.tag])


def record_url(asset) -> str:
    return reverse("web:cybersecurity_update_record", args=[asset.tag])


def _row(update, *, can_view_wo: bool, can_confirm: bool, can_correct: bool) -> dict:
    state = rules.update_state(update)
    number = update.work_order.number if update.work_order_id else ""
    return {"pk": update.pk, "state": state, "label": rules.STATE_LABELS[state], "tone": TONES[state], "words": rules.update_words(update),
            "number": number, "wo_url": reverse("web:wo", args=[number]) if number and can_view_wo else "",
            "confirm_url": (reverse("web:cybersecurity_update_confirm", args=[update.pk])
                            if can_confirm and state == rules.NOT_CONFIRMED else ""),
            "remove_url": reverse("web:cybersecurity_update_remove", args=[update.pk]) if can_correct and state == rules.DONE else ""}


def context(request, asset, today) -> dict:
    """The section's context, under cyber["updates"] (the caller offers the tab only to can_view_devices, never scoped): "rows" (each
    {"pk", "state", "label", "tone", "words", "number", "wo_url", "confirm_url", "remove_url"}), "can_open" and "open_url", "can_record"
    and "record_url", and "empty"."""
    user = request.user
    on_site = asset.status != AssetStatus.RETIRED
    can_edit = cyber_perms.can_edit_devices(user)
    can_view_wo = user.has_level(Module.WORKORDERS, Level.VIEW)
    can_correct = cyber_perms.can_correct_updates(user)
    rows = [_row(u, can_view_wo=can_view_wo, can_confirm=can_edit and on_site, can_correct=can_correct) for u in rules.updates_of(asset)]
    return {"rows": rows, "empty": EMPTY,
            "can_open": cyber_perms.can_open_updates(user) and on_site and not asset.incident_hold, "open_url": open_url(asset),
            "can_record": can_edit and on_site, "record_url": record_url(asset)}
