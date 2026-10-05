"""
Scan tag over the API (slice 19, part C): GET /api/v1/scan/?code=<what a scanner read> returns the device the code names, among
the devices the user may see (apps.equipment.scan, as the Equipment screen's Scan tag reads it). Scoped users are admitted, narrowed
to their share.
"""
from rest_framework import status
from rest_framework.response import Response

from .base import ApiViewSet


class ScanViewSet(ApiViewSet):
    module = "equipment"
    scoped_actions = frozenset({"list"})

    def list(self, request):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)  # built in slice 19, part C


def register(router):
    router.register("scan", ScanViewSet, basename="scan")
