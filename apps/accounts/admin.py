from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.core.admin import TenantModelAdmin

from .models import Role, RolePermission, User


class RolePermissionInline(admin.TabularInline):
    model = RolePermission
    extra = 0
    can_delete = False


@admin.register(Role)
class RoleAdmin(TenantModelAdmin):
    list_display = ("name", "slug", "is_system", "description")
    inlines = [RolePermissionInline]

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            if obj.tenant_id is None:
                obj.tenant = form.instance.tenant
            obj.save()
        formset.save_m2m()


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "tenant", "role", "is_active", "is_invited")
    list_filter = ("tenant", "role", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (("Cadence", {"fields": ("tenant", "role", "department", "company", "phone", "is_invited")}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Cadence", {"fields": ("tenant", "role", "email", "first_name", "last_name")}),)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(tenant_id=request.user.tenant_id)

    def has_delete_permission(self, request, obj=None):
        # Rows in several facilities' tables point at it, and under row-level security a delete only sees the facility it runs as,
        # so it would fail at commit (and the confirmation page would leave out what it deletes).
        # Deactivate a user instead (Users and access).
        return False

