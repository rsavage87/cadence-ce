"""
The survey binder (slice 25, apps.reports.survey) over the API, with the Reports screen's doors (apps.reports.permissions:
Reports View, and each section's own View through survey_refusal). A stub until its build agent fills it in:

GET /api/v1/survey/?from=&to=            The binder: period, sections (figures, gaps, tables without rows), left_out.
GET /api/v1/survey/<section>/?from=&to=  One section with its tables' rows.
"""
from rest_framework.response import Response

from apps.reports import permissions as rep_perms

from .base import ApiViewSet


class SurveyViewSet(ApiViewSet):
    module = rep_perms.MODULE

    def list(self, request):
        return Response({})


def register(router):
    router.register("survey", SurveyViewSet, basename="survey")
