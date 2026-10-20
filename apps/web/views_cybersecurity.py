"""
The Device security screen (slice 37, beyond the mock): Fleet (one row per model with devices on site, its head figures, a model's
devices, Set devices to this release), Models (each model's profile and releases), Changes for IT, and the device drawer's Record facts.
Device security View (and Equipment View for every list naming devices); Edit to record; never a scoped user. Every figure and word comes
from apps.cybersecurity.rules; every write from apps.cybersecurity.services. (Scaffold: wave 2 builds the bodies.)
"""
from django.core.exceptions import PermissionDenied

from apps.cybersecurity import permissions as cyber_perms

from .decorators import web_view

VIEW, EDIT = cyber_perms.VIEW_LEVEL, cyber_perms.EDIT_LEVEL
MODULE = cyber_perms.MODULE


def _todo(*_args, **_kwargs):
    raise PermissionDenied  # scaffold: wave 2 builds the screen


fleet = web_view(MODULE, VIEW)(_todo)
fleet_csv = web_view(MODULE, VIEW)(_todo)
devices_csv = web_view(MODULE, VIEW)(_todo)
models_tab = web_view(MODULE, VIEW)(_todo)
model_panel = web_view(MODULE, VIEW)(_todo)
release_new = web_view(MODULE, EDIT)(_todo)
set_release = web_view(MODULE, EDIT)(_todo)
release_edit = web_view(MODULE, EDIT)(_todo)
release_remove = web_view(MODULE, EDIT)(_todo)
changes = web_view(MODULE, VIEW)(_todo)
changes_csv = web_view(MODULE, VIEW)(_todo)
record_facts = web_view(MODULE, EDIT)(_todo)
