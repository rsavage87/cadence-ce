from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import LaborLine, PartLine, ServiceRequest, WorkOrder, WorkOrderNote, WorkOrderStatusHistory


class LaborInline(admin.TabularInline):
    model = LaborLine
    extra = 0


class PartInline(admin.TabularInline):
    model = PartLine
    extra = 0


class NoteInline(admin.TabularInline):
    model = WorkOrderNote
    extra = 0


class HistoryInline(admin.TabularInline):
    model = WorkOrderStatusHistory
    extra = 0
    can_delete = False
    readonly_fields = ("from_status", "to_status", "changed_by", "note", "created_at")


@admin.register(WorkOrder)
class WorkOrderAdmin(TenantModelAdmin):
    list_display = ("number", "asset", "type", "priority", "status", "assigned_to", "opened_on", "due_on", "completed_on")
    list_filter = ("type", "priority", "status", "source", "vendor_service")
    search_fields = ("number", "asset__tag", "problem", "requester")
    autocomplete_fields = ("asset",)
    readonly_fields = TenantModelAdmin.readonly_fields + ("number",)
    inlines = [LaborInline, PartInline, NoteInline, HistoryInline]

    def save_formset(self, request, form, formset, change):
        for obj in formset.save(commit=False):
            if obj.tenant_id is None:
                obj.tenant = form.instance.tenant
            obj.save()
        formset.save_m2m()


@admin.register(ServiceRequest)
class ServiceRequestAdmin(TenantModelAdmin):
    list_display = ("number", "asset", "department", "urgency", "requester_name", "callback", "created_at")
    readonly_fields = TenantModelAdmin.readonly_fields + ("number", "work_order")
