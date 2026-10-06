"""
The signed-in person's facilities over the API (slice 22, apps.accounts.people), what the top bar's facility menu lists. Session
only (PersonPermission): a token is one facility's account and never reads another facility.

GET  /api/v1/facilities/   [{"name", "slug", "current": this session's facility, "invited": a pending invitation not yet joined}]

Scaffold stub: part A fills it in.
"""
from rest_framework.response import Response

from .base import ApiViewSet
from .permissions import PersonPermission


class FacilityViewSet(ApiViewSet):
    permission_classes = [PersonPermission]
    scoped_actions = frozenset({"list"})  # the person's own facilities: nothing of this facility's records

    def list(self, request):
        return Response([])


def register(router):
    router.register("facilities", FacilityViewSet, basename="facility")
