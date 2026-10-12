from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import EngineerTraining, VendorAttestation, VendorEngineer


class _ReadOnly(TenantModelAdmin):
    """Read only: every change goes through apps.vendors.services (the keys, the dates' rules, the history's reasons). Nothing is
    deleted here: an engineer is deactivated, a record entered in error is removed on the Vendor staff tab."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VendorEngineer)
class VendorEngineerAdmin(_ReadOnly):
    list_display = ("name", "company", "is_active")


@admin.register(EngineerTraining)
class EngineerTrainingAdmin(_ReadOnly):
    list_display = ("engineer", "covers", "source", "issued_on", "expires_on")


@admin.register(VendorAttestation)
class VendorAttestationAdmin(_ReadOnly):
    list_display = ("reference", "covers", "valid_from", "valid_through")
