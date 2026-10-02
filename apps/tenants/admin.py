from django.contrib import admin, messages
from django.shortcuts import redirect

from .models import Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "created_at")
    search_fields = ("name", "slug")
    actions = ["work_as_tenant"]

    def has_delete_permission(self, request, obj=None):
        # Rows in several facilities' tables point at it, and under row-level security a delete only sees the facility it runs as,
        # so it would fail at commit (and the confirmation page would leave out what it deletes).
        # Mark the facility inactive instead: its people can no longer sign in.
        return False

    @admin.action(description="Work as this tenant (superusers)")
    def work_as_tenant(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Only superusers can switch tenants.", level=messages.ERROR)
            return
        tenant = queryset.first()
        request.session["tenant_id"] = str(tenant.id)
        self.message_user(request, f"Now working as {tenant.name}.")
        return redirect("admin:index")
