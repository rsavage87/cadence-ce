from django.contrib import admin

from apps.core.admin import TenantModelAdmin

from .models import EvaluationApproval, EvaluationObjective, EvaluationSnapshot, ProgramEvaluation


class _ReadOnly(TenantModelAdmin):
    """Read only: every change goes through apps.evaluations.services (the statuses, the frozen copy, the history's reasons). Nothing is
    deleted: a draft is discarded, an approved evaluation is amended."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProgramEvaluation)
class ProgramEvaluationAdmin(_ReadOnly):
    list_display = ("number", "version", "period_start", "period_end", "status")
    list_filter = ("status",)


@admin.register(EvaluationSnapshot)
class EvaluationSnapshotAdmin(_ReadOnly):
    list_display = ("evaluation", "taken_on")


@admin.register(EvaluationObjective)
class EvaluationObjectiveAdmin(_ReadOnly):
    list_display = ("evaluation", "kind", "position", "result")


@admin.register(EvaluationApproval)
class EvaluationApprovalAdmin(_ReadOnly):
    list_display = ("evaluation", "body", "meeting_on")
