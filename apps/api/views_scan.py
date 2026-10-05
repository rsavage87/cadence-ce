"""
Scan tag over the API (slice 19, part C): the Equipment screen's Scan tag (apps/web/views_scan.py) for a handheld or a phone app.

GET /api/v1/scan/?code=<what a scanner read>
    The device the code names, as GET /api/v1/assets/<id>/ shows it. A code is an asset tag (in any letter case), a printed label's
    request link (/r/<slug>/?asset=<tag>, with or without its scheme and host), or a link to the device's page (/equipment/<tag>/);
    surrounding whitespace and control characters are dropped (apps/equipment/scan.py reads it, as the screen does). Equipment View.
    A code that names none of the user's devices is a 404 with the screen's words: no device with that tag, another facility's
    label, not a tag or a label (or the request form's link), longer than the limit. A missing code is a 400.

Scoped users (apps.workorders.scoping: a vendor technician, a clinical requester) are admitted, as on the screen, and look up only
the devices in their share: a device outside it reads exactly like a tag that does not exist (the same 404 and words), so the answer
never says whether the facility has it.
"""
from django.core.exceptions import ValidationError
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.models import Module
from apps.equipment import scan as scan_rules
from apps.equipment.models import Asset
from apps.tenants.context import get_current_tenant
from apps.workorders import scoping

from . import serializers as s
from .base import ApiViewSet


class ScanViewSet(ApiViewSet):
    module = Module.EQUIPMENT
    scoped_actions = frozenset({"list"})  # narrowed: the lookup is among scoping.assets, the user's own devices

    def list(self, request):
        tenant = get_current_tenant()
        if tenant is None:  # a superuser who has not picked a tenant: no facility's labels to read
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")
        code = scan_rules.clean(request.query_params.get("code", ""))
        if not code:
            raise DRFValidationError({"code": ["Required: what the scanner read (an asset tag, or the link on a device's label)."]})
        mine = scoping.assets(request.user, Asset.objects.select_related("device_model", "department", "contract"))  # as AssetViewSet
        try:
            asset = scan_rules.find(code, slug=tenant.slug, qs=mine)
        except ValidationError as e:
            raise NotFound(e.messages[0]) from None
        return Response(s.AssetSerializer(asset, context={"request": request, "format": self.format_kwarg, "view": self}).data)


def register(router):
    router.register("scan", ScanViewSet, basename="scan")
