from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Alert, AlertMatch


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("external_id", "source", "classification", "manufacturer", "title", "published_on")
    list_filter = ("source", "classification")
    search_fields = ("external_id", "manufacturer", "product", "title")

    def has_delete_permission(self, request, obj=None):
        # Rows in several facilities' tables point at it, and under row-level security a delete only sees the facility it runs as,
        # so it would fail at commit (and the confirmation page would leave out what it deletes).
        # Notices stay: the facilities' matches and recall work orders name them.
        return False


@admin.register(AlertMatch)
class AlertMatchAdmin(TenantModelAdmin):
    # The disposition changes only through recalls.services (set_status, set_note: on the row as it is now, with the transitions and
    # levels), never a form loaded earlier; a match's identity is fixed by matching.
    readonly_fields = ("alert", "device_model", "status", "closed_on", "disposition_note", "created_at", "updated_at")
    list_display = ("alert", "device_model", "status", "closed_on")
    list_filter = ("status",)
    autocomplete_fields = ("device_model",)
