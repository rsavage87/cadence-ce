from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import FacilitySettings


@admin.register(FacilitySettings)
class FacilitySettingsAdmin(TenantModelAdmin):
    list_display = ("__str__", "portal_require_callback", "target_pm_pct", "target_uptime_pct", "target_mttr_days", "updated_at")
