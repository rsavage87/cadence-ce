from django.contrib import admin

from apps.core.admin import TenantModelAdmin
from apps.equipment.models import Asset

from .models import Contract


class CoveredAssetInline(admin.TabularInline):
    model = Asset
    fields = ("tag", "device_model", "department", "status")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name_plural = "Covered devices"


@admin.register(Contract)
class ContractAdmin(TenantModelAdmin):
    list_display = ("reference", "vendor", "type", "coverage", "start_on", "end_on", "annual_cost", "device_count")
    list_filter = ("type", "coverage")
    search_fields = ("reference", "vendor")
    inlines = [CoveredAssetInline]

    @admin.display(description="Devices")
    def device_count(self, obj):
        return obj.covered_assets().count()
