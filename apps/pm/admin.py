from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import AemDecision, PmProcedure


@admin.register(PmProcedure)
class PmProcedureAdmin(TenantModelAdmin):
    list_display = ("code", "name", "source_kind", "estimated_hours", "revision")
    list_filter = ("source_kind",)
    search_fields = ("code", "name", "source_reference")


@admin.register(AemDecision)
class AemDecisionAdmin(TenantModelAdmin):
    """Read-only: AEM cases are proposed, decided, withdrawn, and ended through apps.pm.aem (the PM program's AEM tab), which keeps
    the model's interval, the committee's sign-off, and the devices' next PMs in step."""

    list_display = ("device_model", "interval_months", "oem_interval_months", "status", "proposed_on", "decided_on", "ended_on")
    list_filter = ("status",)
    search_fields = ("device_model__manufacturer", "device_model__model")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
