"""
The Device security screen's Advisories tab (slice 40): Needs attention for advisories and the list (with its CSV), an advisory's page
(drawer partial or full page, as a decision's: views_cyber_decisions), its devices' CSV, and the modals: Record advisory (CVE overlaps
offered before saving), Edit advisory, Add source / Edit source / Record revision / Remove source, Set models (with "Add a version not
on file", which calls services.add_release and re-renders the form from the POST), Remove model, Confirm models, Not applicable here,
Close, Reopen, Entered in error.

Every refusal a GET shows is read with apps.cybersecurity.advisories' lock-free readers (never a writer's helper: FOR UPDATE outside a
transaction is an error on PostgreSQL); every save calls the writer, which reads the same refusal again under its locks. Saves answer with
the advisory's page into #drawer, CHANGED, a toast, and the modal closed (advisory_saved). Levels: Device security View to read
(device lines, responses, decision-derived words, and the devices CSV also need Equipment View: permissions.can_view_devices); the
writers' levels are advisories.py's. Scoped users never.

Slice 40 wave 2A fills these in; the names here and in urls_cyber_advisories are the contract (rules.URLS, the history areas, the
Record decision modal's `from=advisory` answer, and slice 38's update doors reverse them).
"""
from django.http import Http404

from apps.accounts.models import Level, Module

from .decorators import web_view

WAVE = "slice 40 wave 2A"


def advisory_saved(request, advisory, message: str):
    """The answer of a save made from the advisory's page (this module's modals, Record decision `from=advisory`, slice 38's update
    doors `from=advisory`): the advisory's page into #drawer, CHANGED, a toast with `message`, the modal closed."""
    raise NotImplementedError(WAVE)


def _todo(request, *args, **kwargs):
    raise Http404(WAVE)


advisories_tab = web_view(Module.CYBERSECURITY, Level.VIEW)(_todo)
advisories_csv = web_view(Module.CYBERSECURITY, Level.VIEW)(_todo)
advisory_new = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory = web_view(Module.CYBERSECURITY, Level.VIEW)(_todo)
advisory_edit = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
source_new = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
source_edit = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
source_revision = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
source_remove = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory_models = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
model_remove = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory_confirm = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory_not_applicable = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory_close = web_view(Module.CYBERSECURITY, Level.APPROVE)(_todo)
advisory_reopen = web_view(Module.CYBERSECURITY, Level.EDIT)(_todo)
advisory_in_error = web_view(Module.CYBERSECURITY, Level.APPROVE)(_todo)
advisory_devices_csv = web_view(Module.CYBERSECURITY, Level.VIEW)(_todo)
