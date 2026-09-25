from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Alert, AlertMatch


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("external_id", "source", "classification", "manufacturer", "title", "published_on")
    list_filter = ("source", "classification")
    search_fields = ("external_id", "manufacturer", "product", "title")


@admin.register(AlertMatch)
class AlertMatchAdmin(TenantModelAdmin):
    list_display = ("alert", "device_model", "status", "closed_on")
    list_filter = ("status",)
    autocomplete_fields = ("device_model",)
