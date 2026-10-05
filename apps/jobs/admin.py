from django.contrib import admin

from .models import JobRun


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    """Read-only, and for superusers only: the runs cover every tenant. Slice 21: a facility's jobs run once per facility and its
    local day, so each row names its facility (empty for the openFDA import, which is everyone's) and the day on that clock."""

    list_display = ("job", "facility", "run_on", "status", "started_at", "finished_at")
    list_filter = ("job", "status", "facility")
    list_select_related = ("facility",)  # a system table, like this one: readable as the runtime role with no tenant set
    readonly_fields = ("job", "facility", "run_on", "status", "started_at", "finished_at", "output")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # deleting a row lets its job run again today (a dead "running" row is taken over after 6 h anyway)
