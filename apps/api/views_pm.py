"""
The PM schedule and program (slice 19, part B): the calendar, a day, creating its work orders, Auto-assign week, procedures, risk scores, and AEM.
"""

from datetime import date

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.pm import permissions as pm_perms
from apps.pm import schedule as pm_schedule
from apps.pm.services import create_pm_work_orders_for_day, generate_pm_work_orders
from apps.tenants.context import get_current_tenant
from apps.workorders import permissions as wo_perms

from .base import _hours, _parse_day
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin


class PmViewSet(TenantAPIMixin, viewsets.ViewSet):
    """The PM schedule as JSON (slice 9): GET calendar/?y=&m= (the month grid), GET day/?day=YYYY-MM-DD (the day plan with each
    device's suggested technician), POST create-for-day/ {"day": ...} (one PM work order per device due that day without an open
    one; PM Approve, like the nightly `generate`). Reads need PM View. The numbers come from apps.pm.schedule and the batch from
    apps.pm.services, as on the web screen."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "pm"
    write_level = pm_perms.CREATE_LEVEL
    MIN_YEAR, MAX_YEAR = 1900, 2200  # the grid runs a few days past the month either side; far-out years are typos, not schedules

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:  # a superuser who has not picked a tenant would otherwise read or plan nobody's work
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")

    @action(detail=False, methods=["post"])
    def generate(self, request):
        return Response({"created": generate_pm_work_orders()})

    @action(detail=False, methods=["get"])
    def calendar(self, request):
        today = date.today()
        try:
            year, month = int(request.query_params.get("y", today.year)), int(request.query_params.get("m", today.month))
        except (TypeError, ValueError):
            return Response({"detail": "y and m must be whole numbers."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12 and self.MIN_YEAR <= year <= self.MAX_YEAR):
            return Response({"detail": f"m must be 1 to 12 and y {self.MIN_YEAR} to {self.MAX_YEAR}."}, status=status.HTTP_400_BAD_REQUEST)
        cal = pm_schedule.month_calendar(year, month, today)
        weeks = [[{"date": c["date"].isoformat(), "in_month": c["in_month"], "is_today": c["is_today"], "past": c["past"], "n": c["n"],
                   "life_support": c["life_support"], "high": c["high"]} for c in week] for week in cal["weeks"]]
        return Response({"year": year, "month": month, "due_this_month": cal["due_this_month"], "weeks": weeks})

    @action(detail=False, methods=["get"])
    def day(self, request):
        day = _parse_day(request.query_params.get("day"))
        if day is None:
            raise DRFValidationError({"day": "Required, as YYYY-MM-DD."})
        plan = pm_schedule.day_plan(day, date.today())
        devices = []
        for r in plan["rows"]:
            a, dm, tech = r["asset"], r["asset"].device_model, r["technician"]
            devices.append({"asset_id": str(a.id), "tag": a.tag, "description": dm.description, "manufacturer": dm.manufacturer, "model": dm.model,
                            "department": a.department.name, "risk_class": dm.risk_class, "hours": _hours(r["hours"]),
                            "procedure": r["procedure"].code if r["procedure"] else None, "has_open_pm": r["has_open_pm"],
                            "technician": {"id": str(tech.id), "name": tech.name} if tech else None})
        return Response({"day": day.isoformat(), "overdue": plan["overdue"], "count": plan["count"], "hours": _hours(plan["hours"]),
                         "to_create": plan["to_create"], "devices": devices})

    @action(detail=False, methods=["post"], url_path="create-for-day")
    def create_for_day(self, request):
        day = _parse_day(request.data.get("day") if hasattr(request.data, "get") else None)  # a JSON list or scalar body has no day
        if day is None:
            raise DRFValidationError({"day": "Required, as YYYY-MM-DD."})
        # Creating needs PM Approve (write_level); assigning each work order also needs work-order Approve, as on the screen.
        batch = create_pm_work_orders_for_day(day, by=request.user, assign_to_technicians=wo_perms.can_assign(request.user), today=date.today())
        return Response({"created": batch.created, "assigned": batch.assigned, "skipped": batch.skipped})


def register(router):
    router.register("pm", PmViewSet, basename="pm")
