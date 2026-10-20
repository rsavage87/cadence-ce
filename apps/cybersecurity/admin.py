from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import DeviceSecurity, SoftwareRelease


class _ReadOnly(TenantModelAdmin):
    """Read only: every change goes through apps.cybersecurity.services (the checks, the release lock, the history's reasons). Nothing
    is deleted here: a release entered in error is marked."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SoftwareRelease)
class SoftwareReleaseAdmin(_ReadOnly):
    list_display = ("device_model", "version", "os_family", "mds2", "removed_on")


@admin.register(DeviceSecurity)
class DeviceSecurityAdmin(_ReadOnly):
    list_display = ("asset", "release", "release_since", "checked_on")
