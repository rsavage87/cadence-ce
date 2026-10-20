"""
Software updates on the device drawer's Security tab and the Device security screen's model panel (slice 38; the rules are
apps.cybersecurity.rules, every write apps.cybersecurity.updates): Open software update (one device), Record update (an install with no
work order), Confirm version (a completed update work order with nothing recorded), Entered in error, and Open updates to this release
(the batch, from a release row of the model panel). Views parse input (forms_cyber_updates.py), call the service, and answer with the
drawer or the panel and a toast in rules' words; the levels are checked here and again inside the services. Never a scoped user's
(web_view). Wave 2 builds these.
"""
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import Level, Module

from .decorators import web_view

MODULE = Module.CYBERSECURITY
NOT_BUILT = 501  # scaffold: wave 2 fills these views


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_open(request, tag):
    """Open software update for the device `tag` (GET the modal, POST it): permissions.can_open_updates; updates.open_update."""
    return HttpResponse(status=NOT_BUILT)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_record(request, tag):
    """Record update for the device `tag`, an install with no work order: permissions.can_edit_devices; updates.record."""
    return HttpResponse(status=NOT_BUILT)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def update_confirm(request, pk):
    """Confirm version on the update `pk` (a completed or closed work order's, nothing recorded): permissions.can_edit_devices;
    updates.confirm."""
    return HttpResponse(status=NOT_BUILT)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.APPROVE)
def update_remove(request, pk):
    """Entered in error on the update `pk`: permissions.can_correct_updates; updates.remove."""
    return HttpResponse(status=NOT_BUILT)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, Level.EDIT)
def release_open_updates(request, pk):
    """Open updates to the release `pk` (the batch, from the model panel's release row): permissions.can_open_updates;
    updates.open_updates."""
    return HttpResponse(status=NOT_BUILT)
