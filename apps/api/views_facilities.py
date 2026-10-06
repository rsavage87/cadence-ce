"""
The signed-in person's facilities over the API (slice 22, apps.accounts.people), what the top bar's facility menu lists. Session
only (PersonPermission): a token is one facility's account and never reads another facility.

GET  /api/v1/facilities/   [{"name", "slug", "current": this session's facility, "invited": a pending invitation not yet joined}]
                           By facility name. The facilities people.facility_menu lists: this one, each other facility the person
                           has joined and can sign in to, and each invitation they can join (by switching in the web app). An
                           account in one facility, or a person with nowhere else to go, gets just this facility. Reads User and
                           Tenant only, never another facility's records; no account ids (switching is the web app's).
"""
from rest_framework.response import Response

from apps.accounts import people
from apps.tenants.context import get_current_tenant

from .base import ApiViewSet
from .permissions import PersonPermission

FIELDS = ("name", "slug", "current", "invited")


class FacilityViewSet(ApiViewSet):
    permission_classes = [PersonPermission]
    scoped_actions = frozenset({"list"})  # the person's own facilities: nothing of this facility's records

    def list(self, request):
        rows = people.facility_menu(request.user)
        if not rows:
            tenant = get_current_tenant()  # TenantAPIMixin refused a session with none
            rows = [{"name": tenant.name, "slug": tenant.slug, "current": True, "invited": False}]
        return Response([{key: row[key] for key in FIELDS} for row in rows])


def register(router):
    router.register("facilities", FacilityViewSet, basename="facility")
