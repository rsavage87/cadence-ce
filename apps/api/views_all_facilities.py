"""
All facilities over the API (slice 22): the figures of the web page of that name (apps.reports.all_facilities) for every facility the
signed-in person has joined and may see the Overview of. Session only (PersonPermission): a token never reads another facility.

GET  /api/v1/overview/all-facilities/

Scaffold stub: part B fills it in.
"""
from rest_framework.response import Response

from .base import ApiViewSet
from .permissions import PersonPermission


class AllFacilitiesViewSet(ApiViewSet):
    permission_classes = [PersonPermission]
    scoped_actions = frozenset({"list"})  # each facility is read with the person's account there, never with this one's share

    def list(self, request):
        return Response({"facilities": [], "totals": None})


def register(router):
    router.register("overview/all-facilities", AllFacilitiesViewSet, basename="all-facilities")
