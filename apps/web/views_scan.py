"""
Scan tag (slice 18, the mock's button on Equipment): open a device from its label. A handheld scanner types the code into the
modal's field (keyboard wedge), a phone or laptop camera reads it where the browser can (BarcodeDetector), or someone types the
tag. The code is a tag, or the request link a printed label's QR code carries (/r/<slug>/?tag=...). It resolves among the devices
the user may see (apps.workorders.scoping) and opens that device's drawer.
"""
from django.http import HttpResponse

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def scan(request):
    return HttpResponse(status=501)  # built in slice 18, part B
