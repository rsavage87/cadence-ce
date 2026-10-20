"""
Every write to the device security register (slice 37): a model's profile and releases, and a device's facts. Each validates, resolves
ids through the tenant (Model.objects), writes history with a reason, and refuses in words keyed by field (a ValidationError).

Lock order (one writer, no deadlock): a release row first (select_for_update) when editing, removing, or naming it, then device security
rows in asset pk order. These writers never lock work orders or devices. The one outside caller is equipment.services.update_asset ->
model_changed, which runs with the device's work and row already locked (its work, its row, then its security row, never the reverse).
Slice 38's update events, recorded at a work order's completion, take the security row last too.

Nothing here is deleted: a release entered in error is marked (removed_on, removed_by) and refused while a device names it; adding the
same version again restores it. A device's facts stay with its history when it is retired; a retired device (or a returned temporary one)
is refused, in words.
"""
from __future__ import annotations

import re
from datetime import date

from django.db import transaction

from .models import DeviceSecurity

KEEP = object()  # an argument left as it is
BULK_MAX = 500  # devices set to a release at once (one model's)
EARLIEST_YEAR = 1950  # a date before it is refused (a mistyped year)
HORIZON_YEARS = 50  # a date further ahead is refused (a "never" placeholder)
CREDENTIAL_QUERY_KEYS = ("token", "key", "sig", "signature", "auth", "session", "password", "passwd", "pwd", "x-amz-")

DEVICE_MODEL_CHANGED = "Device model changed"


def version_key(version: str) -> str:
    """The uniqueness of a version in any letter case: casefolded, trimmed, runs of spaces collapsed, one leading "v", "rev", or "rev."
    dropped when a digit follows ("V12.1.2", "Rev. 12.1.2", and "12.1.2" are one release). Blank for the profile."""
    text = " ".join((version or "").split()).casefold()
    return re.sub(r"^(?:rev\.?|v)\s*(?=\d)", "", text)


def add_release(model, *, by, today: date | None = None, **fields):
    """A versioned release of `model` (or its profile, version blank: at most one). Re-adding a version entered in error restores it."""
    raise NotImplementedError


def update_release(release, *, by, today: date | None = None, **fields):
    """Change a release's (or the profile's) fields; the version only while no device names it."""
    raise NotImplementedError


def remove_release(release, *, by, today: date | None = None):
    """Entered in error: refused while a device names it (or for the profile)."""
    raise NotImplementedError


def record_facts(asset, *, by, today: date | None = None, release=KEEP, release_since=KEEP, network=KEEP, checked_on=None):
    """One device's facts: the versioned release of its model it runs (None clears it), since when (today by default when the release
    changes), its network fact (blank: as the model discloses), and the day checked (today by default; never after today)."""
    raise NotImplementedError


def record_facts_for(model, assets, *, by, today: date | None = None, release, release_since=None, network=KEEP):
    """Set one model's devices (at most BULK_MAX, all of `model`, on site) to `release`, each saved with history, in one transaction."""
    raise NotImplementedError


@transaction.atomic
def model_changed(asset, *, by=None) -> bool:
    """A device's model changed (equipment.services.update_asset, with its work and row locked): a release of its old model no longer
    describes it, so the release and its day are cleared, with history. Returns whether one was cleared."""
    sec = DeviceSecurity.objects.select_for_update().filter(asset=asset).first()
    if sec is None or sec.release_id is None or sec.release.device_model_id == asset.device_model_id:
        return False
    sec.release, sec.release_since = None, None
    _save(sec, by, DEVICE_MODEL_CHANGED)
    return True


def _save(obj, by, reason: str) -> None:
    """Save `obj` with `reason` in its history (cut to the column), as `by`; neither reaches a later save of this copy."""
    obj._change_reason = reason[:type(obj).history.model._meta.get_field("history_change_reason").max_length]
    if by is not None:
        obj._history_user = by
    try:
        obj.save()
    finally:
        obj.__dict__.pop("_change_reason", None)
        obj.__dict__.pop("_history_user", None)
