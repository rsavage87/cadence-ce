from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Asset, Department, DeviceModel


@admin.register(Department)
class DepartmentAdmin(TenantModelAdmin):
    list_display = ("name", "cost_center")
    search_fields = ("name",)


@admin.register(DeviceModel)
class DeviceModelAdmin(TenantModelAdmin):
    list_display = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "aem_interval_months")
    list_filter = ("risk_class", "category", "manufacturer")
    search_fields = ("manufacturer", "model", "description")


@admin.register(Asset)
class AssetAdmin(TenantModelAdmin):
    list_display = ("tag", "device_model", "department", "status", "next_pm_on", "support_type", "contract")
    list_filter = ("status", "support_type", "device_model__risk_class", "department")
    search_fields = ("tag", "serial", "device_model__model", "device_model__manufacturer")
    autocomplete_fields = ("device_model", "department", "contract")
    readonly_fields = TenantModelAdmin.readonly_fields + ("support_type",)
