from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import PmProcedure


@admin.register(PmProcedure)
class PmProcedureAdmin(TenantModelAdmin):
    list_display = ("code", "name", "source_kind", "estimated_hours", "revision")
    list_filter = ("source_kind",)
    search_fields = ("code", "name", "source_reference")
