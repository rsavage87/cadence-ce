from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import Credential, Technician


class CredentialInline(admin.TabularInline):
    model = Credential
    extra = 0


@admin.register(Technician)
class TechnicianAdmin(TenantModelAdmin):
    list_display = ("name", "title", "certification", "weekly_capacity_hours", "is_active")
    search_fields = ("name",)
    inlines = [CredentialInline]

    def save_formset(self, request, form, formset, change):
        for obj in formset.save(commit=False):
            if obj.tenant_id is None:
                obj.tenant = form.instance.tenant
            obj.save()
        formset.save_m2m()
