from django.contrib import admin

from apps.tenants.context import get_current_tenant


class TenantModelAdmin(admin.ModelAdmin):
    """Base admin for tenant-scoped models: rows are already scoped by the manager; new rows get the current tenant."""

    readonly_fields = ("created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if obj.tenant_id is None:
            obj.tenant = get_current_tenant()
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request):
        return get_current_tenant() is not None or request.user.is_superuser
