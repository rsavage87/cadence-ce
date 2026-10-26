from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import AdvisoryModel, AdvisoryRelease, AdvisorySource, DeviceSecurity, DeviceSecurityDecision, SecurityAdvisory, SoftwareRelease


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


@admin.register(DeviceSecurityDecision)
class DeviceSecurityDecisionAdmin(_ReadOnly):  # slice 39: every change through apps.cybersecurity.decisions
    list_display = ("number", "applies_to", "advisory", "asset", "device_model", "release", "response", "review_by", "ended_on")


@admin.register(SecurityAdvisory)
class SecurityAdvisoryAdmin(_ReadOnly):  # slice 40: every change through apps.cybersecurity.advisories
    list_display = ("number", "received_on", "status", "not_applicable_reason", "models_confirmed_on", "removed_on")


@admin.register(AdvisorySource)
class AdvisorySourceAdmin(_ReadOnly):
    list_display = ("advisory", "source", "reference", "published_on", "updated_on", "revision", "removed_on")


@admin.register(AdvisoryModel)
class AdvisoryModelAdmin(_ReadOnly):
    list_display = ("advisory", "device_model", "versions", "remedy", "fix_release")


@admin.register(AdvisoryRelease)
class AdvisoryReleaseAdmin(_ReadOnly):
    list_display = ("advisory", "advisory_model", "release")
