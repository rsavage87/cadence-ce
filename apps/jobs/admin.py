from django.contrib import admin

from .models import JobRun


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    """Read-only, and for superusers only: the runs cover every tenant."""

    list_display = ("job", "run_on", "status", "started_at", "finished_at")
    list_filter = ("job", "status")
    readonly_fields = ("job", "run_on", "status", "started_at", "finished_at", "output")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # clearing a stuck "running" row lets the job run again today
