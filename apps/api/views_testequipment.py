"""
The CE department's test equipment (slice 35) over the API: read only (adding, changing, calibrations, and reviews are the Equipment
screen's Test equipment tab's, through apps.testequipment.services). Equipment View (apps.testequipment.permissions.can_view: the
screen's door), checked server-side on every request; Module.EQUIPMENT and no scoped_actions: a vendor's or a requester's account is
refused every endpoint (403), whatever its levels.

A stub until slice 35's wave 2 (build35.md, "API"): list and retrieve by id or tag, the standing judged today, calibrations nested on
retrieve, reviews.
"""
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.testequipment import permissions as perms
from apps.testequipment.models import TestEquipment

from .permissions import ModulePermission
from .tenancy import TenantAPIMixin


class _StubSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestEquipment
        fields = ["id", "tag"]


class TestEquipmentViewSet(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """The test equipment (the module docstring)."""
    __test__ = False  # pytest: not a test class

    permission_classes = [IsAuthenticated, ModulePermission]
    module = perms.MODULE
    scoped_actions = frozenset()  # never a scoped user's
    serializer_class = _StubSerializer

    def get_queryset(self):
        return TestEquipment.objects.order_by("tag")


def register(router) -> None:
    router.register("test-equipment", TestEquipmentViewSet, basename="test-equipment")
