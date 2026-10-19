"""
The CE department's test equipment (slice 35): the Equipment screen's Test equipment tab, an item's drawer (a full page when opened
directly), Add test equipment, Change, the status moves, Record calibration, entered in error, the tolerance review, and the CSVs.
Views parse input and call apps.testequipment.services; the rule is apps.testequipment.rules; the levels apps.testequipment.permissions
(View: Equipment View; the views check the finer levels and the services again). Scoped users are refused every view here (web_view's
default).

Stubs until slice 35's wave 2: every URL name exists and every view goes through web_view, so the scoping walk is green from the start.
"""
from django.http import HttpResponse

from apps.accounts.models import Level, Module

from .decorators import web_view


def _stub(request, *args, **kwargs):
    return HttpResponse("Test equipment (slice 35): not built yet.", status=501)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment(request):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_new(request):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_csv(request):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_item(request, tag):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_edit(request, tag):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_status(request, tag):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_uses_csv(request, tag):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_calibrate(request, tag):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_calibration_error(request, tag, pk):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_review(request, tag, pk):
    return _stub(request)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_review_csv(request, tag, pk):
    return _stub(request)
