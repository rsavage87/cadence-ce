"""
A work order's labor, parts, and notes (slice 19, part A), as the work order drawer records them: nested under the work order,
/api/v1/work-orders/<id>/labor/, .../parts/, .../notes/. Scoped users (apps.workorders.scoping) reach these for the work orders in
their share, as the web's Log time, Add part, and notes admit them.
"""
from rest_framework import status
from rest_framework.response import Response

from .base import ApiViewSet

WORK_ORDER = r"work-orders/(?P<work_order_pk>[^/.]+)"


class _Stub(ApiViewSet):
    module = "workorders"

    def list(self, request, work_order_pk=None):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)  # built in slice 19, part A

    def create(self, request, work_order_pk=None):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)


class WorkOrderLaborViewSet(_Stub):
    scoped_actions = frozenset({"list", "create", "destroy"})

    def destroy(self, request, work_order_pk=None, pk=None):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)


class WorkOrderPartViewSet(_Stub):
    scoped_actions = frozenset({"list", "create", "destroy"})

    def destroy(self, request, work_order_pk=None, pk=None):
        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)


class WorkOrderNoteViewSet(_Stub):
    scoped_actions = frozenset({"list", "create"})


def register(router):
    router.register(f"{WORK_ORDER}/labor", WorkOrderLaborViewSet, basename="workorder-labor")
    router.register(f"{WORK_ORDER}/parts", WorkOrderPartViewSet, basename="workorder-part")
    router.register(f"{WORK_ORDER}/notes", WorkOrderNoteViewSet, basename="workorder-note")
