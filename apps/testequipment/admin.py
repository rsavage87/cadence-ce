from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Calibration, TestEquipment, TestEquipmentUse, ToleranceReview


class _ReadOnly(TenantModelAdmin):
    """Read only: every change goes through apps.testequipment.services (the tag lock, the status rules, the history's reasons) and,
    for uses, the work order's completion. Nothing is deleted here: a calibration entered in error is marked, an item is retired."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TestEquipment)
class TestEquipmentAdmin(_ReadOnly):
    list_display = ("tag", "kind", "manufacturer", "model", "serial", "status")


@admin.register(Calibration)
class CalibrationAdmin(_ReadOnly):
    list_display = ("test_equipment", "calibrated_on", "due_on", "provider", "certificate", "as_found", "result", "entered_in_error")


@admin.register(ToleranceReview)
class ToleranceReviewAdmin(_ReadOnly):
    list_display = ("calibration", "basis", "decided_on", "work_count")


@admin.register(TestEquipmentUse)
class TestEquipmentUseAdmin(_ReadOnly):
    list_display = ("work_order", "test_equipment")
