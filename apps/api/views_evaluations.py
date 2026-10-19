"""
The program's annual evaluation (slice 32) over the API: read-only this slice (writes are the screens', Reports, Program evaluation,
through apps.evaluations.services). Evaluations View (apps.evaluations.permissions), checked server-side on every request;
Module.EVALUATIONS and no scoped_actions: a vendor's or a requester's account is refused every endpoint (403), whatever its levels.

GET program-evaluations/                  Evaluations View. Newest period first (then the latest version), 50 a page, each
                                          without figures or text: number, version, label, the months, status, the day sent to
                                          committee and the day approved, the version it amends, the committees that approved it
                                          ({body, committee, meeting_on}), and its page on the web naming the facility.
                                          ?status= draft | ready | approved | superseded | discarded | all (the default);
                                          ?search= a number; ?ordering= period_start, period_end, number, approved_on (a leading "-"
                                          reverses it).
GET program-evaluations/<id or number>/   Evaluations View. One evaluation (serializers_evaluations.EvaluationSerializer): the
                                          list's fields, the text, the inventory review, the plan reviewed, the objectives, every
                                          approval, and the figures cut to the reader (apps.evaluations.indicators.cut: left_out names
                                          what their role may not see; without Reports View, no figures at all). By number (EV-26-0001,
                                          any letter case): its version of record (services.current: the approved one while an
                                          amendment is drafted); ?version=<n> a given version.

Another facility's evaluation is a 404, a level missing a 403 in plain words, and any write a 403 (without Edit) or a 405.
"""
import uuid

from django.db.models import Prefetch
from rest_framework import viewsets
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated

from apps.evaluations import permissions as perms
from apps.evaluations import services as ev_services
from apps.evaluations.models import EvaluationApproval, EvaluationObjective, ProgramEvaluation

from . import serializers_evaluations as se
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

VIEW_REFUSAL = "Reading program evaluations needs Evaluations View."
WRITE_REFUSAL = ("The API reads program evaluations only. Drafting and changing one is done on the Program evaluation screen, and needs "
                 "Evaluations Edit.")
NOT_FOUND = "No program evaluation with that id or number in this facility."
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


class ProgramEvaluationViewSet(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """The evaluations (the module docstring). Reads need Evaluations View; the figures also each reader's own areas (the cut)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = perms.MODULE
    delete_level = perms.EDIT_LEVEL  # review fix: a DELETE is a write like any other: Edit gets the 405, never a 403 naming a level held
    scoped_actions = frozenset()  # never a scoped user's: the evaluation covers the whole facility
    search_fields = ["number"]
    ordering_fields = ["period_start", "period_end", "number", "approved_on"]

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in plain words (a write: Edit, the default write_level, which a read-only endpoint keeps so
        that a write added later is closed until its level is set); the scoped refusal keeps its own. With the level, a write is a
        405: no handler takes it."""
        if message is None and request.user and request.user.is_authenticated:
            message = VIEW_REFUSAL if request.method in SAFE_METHODS else WRITE_REFUSAL
        super().permission_denied(request, message=message, code=code)

    def get_serializer_class(self):
        return se.EvaluationListSerializer if self.action == "list" else se.EvaluationSerializer

    def get_queryset(self):
        approvals = EvaluationApproval.objects.order_by("meeting_on", "created_at")
        if self.action != "list":
            objectives = EvaluationObjective.objects.order_by("kind", "position", "created_at")
            return (ProgramEvaluation.objects.select_related("amends", "created_by", "snapshot__taken_by")
                    .prefetch_related(Prefetch("approvals", queryset=approvals.select_related("recorded_by")),
                                      Prefetch("objectives", queryset=objectives)))
        qs = ProgramEvaluation.objects.select_related("amends").prefetch_related(Prefetch("approvals", queryset=approvals))
        wanted = self.request.query_params.get("status") or "all"
        if wanted not in se.STATUS_FILTERS:
            raise DRFValidationError({"status": [f"One of {', '.join(se.STATUS_FILTERS)}."]})
        if wanted != "all":
            qs = qs.filter(status=wanted)
        return qs.order_by("-period_end", "-version")

    def get_object(self):
        """By id, or by number in any letter case: its version of record (services.current), or ?version=<n>. Another facility's is a
        404, as an unknown one."""
        value = str(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field) or "")
        qs = self.get_queryset()
        pk = _uuid(value)
        if pk is not None:
            found = qs.filter(pk=pk).first()
        else:
            number = value.strip().upper()
            version = self.request.query_params.get("version")
            if version not in (None, ""):
                if not (version.isascii() and version.isdigit() and 0 < int(version) < 1000):
                    raise DRFValidationError({"version": ["A version number, such as 2."]})
                found = qs.filter(number=number, version=int(version)).first()
            else:
                shown = ev_services.current(number)
                found = qs.filter(pk=shown.pk).first() if shown is not None else None
        if found is None:
            raise NotFound(NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found


def register(router) -> None:
    router.register("program-evaluations", ProgramEvaluationViewSet, basename="program-evaluation")
