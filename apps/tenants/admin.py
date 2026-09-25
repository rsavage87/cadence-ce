from django.contrib import admin, messages
from django.shortcuts import redirect

from .models import Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "created_at")
    search_fields = ("name", "slug")
    actions = ["work_as_tenant"]

    @admin.action(description="Work as this tenant (superusers)")
    def work_as_tenant(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Only superusers can switch tenants.", level=messages.ERROR)
            return
        tenant = queryset.first()
        request.session["tenant_id"] = str(tenant.id)
        self.message_user(request, f"Now working as {tenant.name}.")
        return redirect("admin:index")
