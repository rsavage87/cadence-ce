"""
Which test equipment a work order recorded (slice 35): the completion modal's "Test equipment used" picker, the drawer's "Test
equipment" row (each item with its standing on the day of the work), and its Change (work-orders/<number>/test-equipment/, posting to
workorders.services.set_test_equipment). Never a scoped user's. The rule is apps.testequipment.rules.

A stub until slice 35's wave 2.
"""
from django.http import HttpResponse

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.WORKORDERS, Level.EDIT)
def wo_test_equipment(request, number):
    return HttpResponse("Test equipment used (slice 35): not built yet.", status=501)
