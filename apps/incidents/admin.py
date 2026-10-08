from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Incident, IncidentHold


@admin.register(Incident)
class IncidentAdmin(TenantModelAdmin):
    """Read only: every change goes through apps.incidents.services (its rules, the device's hold, the history's reasons)."""
    list_display = ("number", "asset", "occurred_on", "outcome", "status")
    list_filter = ("status", "outcome")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # 803.18 keeps an event file two years; "recorded in error" is the way out


@admin.register(IncidentHold)
class IncidentHoldAdmin(TenantModelAdmin):
    list_display = ("incident", "asset", "held_on", "released_on", "release")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
