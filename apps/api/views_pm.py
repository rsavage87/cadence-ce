"""
The PM schedule and the PM program over the API (slice 9; slice 19, part B): the calendar, a day, creating its work orders,
Auto-assign week, PM procedures, a device model's procedure, risk score, and AEM program, and the AEM decisions. Each endpoint has
the door of the web screen that does the same thing (apps/web/views_pm.py, views_pm_week.py, views_procedures.py, views_models.py,
views_aem.py), with the levels in apps/pm/permissions.py and apps/equipment/permissions.py, and calls the same service with the
same arguments. Reads need PM View, as the PM schedule and the model drawer do.

The PM schedule, /api/v1/pm/:
  GET  pm/calendar/?y=&m=          PM View. The month grid.
  GET  pm/day/?day=YYYY-MM-DD       PM View. The day plan, each device with its suggested technician.
  POST pm/create-for-day/           PM Approve. {"day": "YYYY-MM-DD"}: one PM work order per device due that day without an open one,
                                    each assigned to the suggested technician when the user may also assign work orders (Work
                                    orders Approve), as the screen's Create (pm.services.create_pm_work_orders_for_day).
  POST pm/generate/                 PM Approve. The nightly generate_pm, now.
  GET  pm/week/                     PM Approve and Work orders Approve (pm_perms.can_assign_week), as the Auto-assign week modal:
                                    what it would do over today through today + 6 (pm.services.week_assignment_preview). Changes
                                    nothing.
  POST pm/assign-week/              The same levels; no body. Does it (pm.services.assign_week, which takes the planner lock first, so
                                    a second request finds nothing left to do) and returns what was done in the preview's shape.

PM procedures, /api/v1/pm-procedures/ (apps.pm.procedures):
  GET   pm-procedures/?search=      PM View. Code, name, source, revision, estimated hours, the checklist as structured lines
  GET   pm-procedures/<id>/         ({"text", "measure"}: measure null, true for a reading without saying what, or what to record)
                                    and as the editor's text, the models that use it, and the open PM work orders on their devices.
  POST  pm-procedures/              PM Edit. {"code", "name", "source_kind", "estimated_hours", "checklist", and optionally
                                    "source_reference", "source_url", "revision", "device_model"}: create_procedure's rules (a code
                                    unique here in any letter case, 0.1 to 40 hours, 1 to 60 steps). The checklist is a list of
                                    steps (text, or {"text", "measure"}) or the editor's text, one step per line, "text | what to
                                    record" for a reading. With "device_model" (an id from this facility), the procedure becomes that
                                    model's too, in one go, as the model drawer's New procedure does. 201.
  PUT   pm-procedures/<id>/         PM Edit. Every field, as Edit procedure saves them: one left out is saved blank (and refused when
                                    it is required).
  PATCH pm-procedures/<id>/         PM Edit. Only the fields given (update_procedure). A revision applies to every model using the
                                    procedure: PM work orders created from then on plan its new hours, while an open PM work order
                                    keeps the hours estimated when it was created and prints the checklist as it is when printed.
                                    The answer is the procedure with "changed" (the fields that changed). No DELETE.

A device model's PM program, /api/v1/device-models/<id>/... (the model drawer's tabs):
  GET    .../procedure/             PM View. {"procedure": its id or null, "procedure_detail", "hours" (what its PMs plan),
                                    "default_hours" (a model without one)}.
  PUT    .../procedure/             PM Edit. {"procedure": an id from this facility's library, or null for none}
                                    (procedures.set_model_procedure). Sending the current one changes nothing ("changed": false).
  GET    .../risk-score/            PM View. The four parts (function, physical, maintenance, incidents; null while unscored), the
                                    score, the band and the class it gives, the class on file, reviewed on, review due.
  PUT    .../risk-score/            Equipment Approve (eq_perms.can_set_risk), as the risk modal. {"function", "physical",
                                    "maintenance", "incidents"}: whole numbers in the rubric's ranges (set_risk_score). The score's
                                    band becomes the model's class; the same score again is the yearly review. The answer adds
                                    "effects": the class before, whether it changed, whether this was the review, and "aem": a model
                                    scored into life support leaves AEM (its interval in force ends, an open proposal is
                                    withdrawn) and "devices_moved" says how many devices' next PM came in.
  DELETE .../risk-score/            Equipment Approve. Back to unscored (clear_risk_score); the class stays. 400 when unscored.
  GET    .../aem/                   PM View. The interval in force, why the model is excluded if it is (apps.pm.aem.exclusion: life
                                    support, or the CMS mark), the decision in force, an interval on file without a recorded
                                    approval, the open proposal, why a proposal would be refused now, the failure history the AEM
                                    tab shows (aem.evidence), and every decision.
  POST   .../aem/                   PM Edit. {"interval_months", "rationale"}: propose (aem.propose: never for an excluded model, one
                                    open proposal at a time, the policy's failure history). 201 with the decision.
  POST   .../aem/end/               PM Approve. {"reason"}: end the AEM interval in force, approved or on file without a recorded
                                    approval (aem.end); back on the OEM interval. The answer is the AEM program with
                                    "devices_moved" (the devices whose next PM came in).

AEM decisions, /api/v1/aem-decisions/ (apps.pm.aem):
  GET  aem-decisions/?device_model=&status=   PM View. Newest first.
  GET  aem-decisions/<id>/
  POST aem-decisions/<id>/approve/  PM Approve, and not the proposer (403: the committee signs off on someone else's case).
                                    {"decided_on": the committee's meeting date, YYYY-MM-DD, "note": the minutes reference}. The
                                    answer adds "devices_moved": the devices whose next PM came in (a shorter interval).
  POST aem-decisions/<id>/reject/   The same door and body; the model keeps its interval.
  POST aem-decisions/<id>/withdraw/ The proposer while they hold PM Edit, or a PM Approve holder; no body.

A refusal is a 403 with a plain detail; a service's ValidationError a 400 keyed by field (base._via_service); another facility's
id in the address a 404, and in a body a 400 (as the API's related fields answer). A write body's unknown fields are refused (400,
"Unknown field."), and so are fields a GET shows but a write never takes, unless sent back unchanged ("Read only."). Every endpoint
here is closed to scoped users (apps.workorders.scoping): the PM program is the facility's.
"""

import uuid
from datetime import date

from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq_services
from apps.equipment.models import DeviceModel
from apps.pm import aem, procedures
from apps.pm import permissions as pm_perms
from apps.pm import schedule as pm_schedule
from apps.pm.models import AemDecision, AemStatus, PmProcedure
from apps.pm.services import assign_week, create_pm_work_orders_for_day, generate_pm_work_orders, week_assignment_preview
from apps.tenants.context import get_current_tenant
from apps.workorders import permissions as wo_perms
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

from . import serializers_pm as s
from .base import ApiViewSet, TenantViewSet, _hours, _parse_day, _via_service
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

# The words of a 403, for ModulePermission's refusal (PmProgramAccess.permission_denied) and the checks in the actions.
PROCEDURE_REFUSAL = "Writing PM procedures needs PM Edit."
CHOOSE_REFUSAL = "Choosing a model's PM procedure needs PM Edit."  # as DeviceModelViewSet's PATCH says it
RISK_REFUSAL = "Scoring a model's risk needs Equipment Approve."
PROPOSE_REFUSAL = "Proposing an AEM interval needs PM Edit."
DECIDE_REFUSAL = "Approving or rejecting an AEM proposal needs PM Approve."
END_REFUSAL = "Ending an AEM interval needs PM Approve."
WITHDRAW_REFUSAL = "Withdrawing an AEM proposal needs PM Approve, or PM Edit for the person who proposed it."
WEEK_REFUSAL = "Auto-assign week needs PM Approve and the right to assign work orders (Work orders Approve)."


def _today() -> date:
    """The PM endpoints' clock, in one place so tests can pin it (the PM screen's is apps.web.views_pm._today): the facility's
    today, since the API works in its time zone (apps.api.tenancy)."""
    return timezone.localdate()


def _require(allowed: bool, message: str) -> None:
    if not allowed:
        raise PermissionDenied(message)


def _body(request):
    """The write's fields: a JSON object (or a form). A JSON list or scalar has no fields."""
    if not hasattr(request.data, "get"):
        raise DRFValidationError({"detail": "Send the fields as a JSON object."})
    return request.data


def _check_fields(data, writable, read_only=(), shown=None, hints=None) -> None:
    """Refuse what this write does not take, rather than drop it (a client would think it saved): an unknown field, or a field
    the GET shows (`read_only`) sent with a value other than the one shown, so sending back what a GET returned is fine."""
    errors = {}
    for key in data:
        if key in writable:
            continue
        if key in read_only:
            if shown is not None and key in shown and data[key] == shown[key]:
                continue
            errors[key] = [(hints or {}).get(key, "Read only.")]
        else:
            errors[key] = ["Unknown field."]
    if errors:
        raise DRFValidationError(errors)


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _in_facility(model, value):
    """The row a body's id names, from the tenant-scoped manager: None for another facility's, or for anything that is not an id."""
    pk = _uuid(value) if isinstance(value, str) else None
    return model.objects.filter(pk=pk).first() if pk else None


def _text(data, key) -> str:
    """A text field of the body: "" when missing or null; anything but text is a 400 on that field (the services expect text)."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DRFValidationError({key: ["Send this as text."]})
    return value


class PmProgramAccess:
    """Every endpoint here: working inside a tenant (a superuser who has not picked one would otherwise read or plan nobody's work),
    and ModulePermission's level refusals in plain words (`refusals`, by action). The scoped refusal keeps its own words."""

    refusals: dict = {}

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")

    def permission_denied(self, request, message=None, code=None):
        if message is None:
            message = self.refusals.get(getattr(self, "action", None))
        super().permission_denied(request, message=message, code=code)


# --- the PM schedule ------------------------------------------------------------------------------------------------------------


class PmViewSet(PmProgramAccess, TenantAPIMixin, viewsets.ViewSet):
    """The PM schedule as JSON (slice 9): GET calendar/?y=&m= (the month grid), GET day/?day=YYYY-MM-DD (the day plan with each
    device's suggested technician), POST create-for-day/ {"day": ...} (one PM work order per device due that day without an open
    one; PM Approve, like the nightly `generate`). Reads need PM View. The numbers come from apps.pm.schedule and the batch from
    apps.pm.services, as on the web screen. Slice 19: GET week/ and POST assign-week/, Auto-assign week (the module docstring)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "pm"
    write_level = pm_perms.CREATE_LEVEL
    refusals = {"assign_week": WEEK_REFUSAL}
    MIN_YEAR, MAX_YEAR = 1900, 2200  # the grid runs a few days past the month either side; far-out years are typos, not schedules

    @action(detail=False, methods=["post"])
    def generate(self, request):
        return Response({"created": generate_pm_work_orders(as_of=_today())})

    @action(detail=False, methods=["get"])
    def calendar(self, request):
        today = _today()
        try:
            year, month = int(request.query_params.get("y", today.year)), int(request.query_params.get("m", today.month))
        except (TypeError, ValueError):
            return Response({"detail": "y and m must be whole numbers."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12 and self.MIN_YEAR <= year <= self.MAX_YEAR):
            return Response({"detail": f"m must be 1 to 12 and y {self.MIN_YEAR} to {self.MAX_YEAR}."}, status=status.HTTP_400_BAD_REQUEST)
        cal = pm_schedule.month_calendar(year, month, today)
        weeks = [[{"date": c["date"].isoformat(), "in_month": c["in_month"], "is_today": c["is_today"], "past": c["past"], "n": c["n"],
                   "life_support": c["life_support"], "high": c["high"]} for c in week] for week in cal["weeks"]]
        return Response({"year": year, "month": month, "due_this_month": cal["due_this_month"], "weeks": weeks})

    @action(detail=False, methods=["get"])
    def day(self, request):
        day = _parse_day(request.query_params.get("day"))
        if day is None:
            raise DRFValidationError({"day": "Required, as YYYY-MM-DD."})
        plan = pm_schedule.day_plan(day, _today())
        devices = []
        for r in plan["rows"]:
            a, dm, tech = r["asset"], r["asset"].device_model, r["technician"]
            devices.append({"asset_id": str(a.id), "tag": a.tag, "description": dm.description, "manufacturer": dm.manufacturer, "model": dm.model,
                            "department": a.department.name, "risk_class": dm.risk_class, "hours": _hours(r["hours"]),
                            "procedure": r["procedure"].code if r["procedure"] else None, "has_open_pm": r["has_open_pm"],
                            "technician": {"id": str(tech.id), "name": tech.name} if tech else None})
        return Response({"day": day.isoformat(), "overdue": plan["overdue"], "count": plan["count"], "hours": _hours(plan["hours"]),
                         "to_create": plan["to_create"], "devices": devices})

    @action(detail=False, methods=["post"], url_path="create-for-day")
    def create_for_day(self, request):
        day = _parse_day(request.data.get("day") if hasattr(request.data, "get") else None)  # a JSON list or scalar body has no day
        if day is None:
            raise DRFValidationError({"day": "Required, as YYYY-MM-DD."})
        # Creating needs PM Approve (write_level); assigning each work order also needs work-order Approve, as on the screen.
        batch = create_pm_work_orders_for_day(day, by=request.user, assign_to_technicians=wo_perms.can_assign(request.user), today=_today())
        return Response({"created": batch.created, "assigned": batch.assigned, "skipped": batch.skipped})

    @action(detail=False, methods=["get"])
    def week(self, request):
        """Auto-assign week's preview. Its modal needs what the button does (can_assign_week), on GET too, as on the screen."""
        _require(pm_perms.can_assign_week(request.user), WEEK_REFUSAL)
        return Response(s.week_data(week_assignment_preview(_today())))

    @action(detail=False, methods=["post"], url_path="assign-week")
    def assign_week(self, request):
        _require(pm_perms.can_assign_week(request.user), WEEK_REFUSAL)
        _check_fields(_body(request), writable=())
        return Response(s.week_data(assign_week(by=request.user, today=_today())))


# --- PM procedures --------------------------------------------------------------------------------------------------------------


class PmProcedureViewSet(PmProgramAccess, TenantViewSet):
    model, module, serializer_class = PmProcedure, "pm", s.ProcedureSerializer
    http_method_names = ["get", "post", "put", "patch", "head", "options"]  # no DELETE: the library keeps its procedures
    write_level = pm_perms.PROGRAM_EDIT_LEVEL
    refusals = {"create": PROCEDURE_REFUSAL, "update": PROCEDURE_REFUSAL, "partial_update": PROCEDURE_REFUSAL}
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name", "estimated_hours", "updated_at"]
    WRITABLE = procedures.EDITABLE
    HINTS = {"checklist_text": "Read only: send the checklist in checklist, as a list of steps or as this text."}

    def get_queryset(self):
        open_pm = (WorkOrder.objects.filter(asset__device_model__pm_procedure=OuterRef("pk"), type=WoType.PM, status__in=OPEN_STATUSES)
                   .order_by().values("asset__device_model__pm_procedure").annotate(n=Count("id")).values("n"))
        users = Prefetch("device_models", queryset=DeviceModel.objects.order_by("manufacturer", "model"))
        return (PmProcedure.objects.prefetch_related(users)
                .annotate(open_pm=Coalesce(Subquery(open_pm, output_field=IntegerField()), 0)).order_by("code"))

    def _fresh(self, procedure):
        return self.get_queryset().get(pk=procedure.pk)

    def _fields(self, data, *, every: bool) -> dict:
        parsed = s.ProcedureInputSerializer(data=data)
        parsed.is_valid(raise_exception=True)
        d = parsed.validated_data
        if every:  # as the web form sends them: every field, blank when left out
            return {f: d.get(f, "") for f in procedures.EDITABLE}
        return {f: d[f] for f in procedures.EDITABLE if f in d}

    def create(self, request, *args, **kwargs):
        """create_procedure; with "device_model", set_model_procedure in the same transaction (a model that cannot take it leaves no
        procedure behind), as the model drawer's New procedure."""
        data = _body(request)
        _check_fields(data, writable=(*self.WRITABLE, "device_model"), read_only=s.PROCEDURE_READ_ONLY, hints=self.HINTS)
        fields = self._fields(data, every=True)
        dm = None
        if data.get("device_model") not in (None, ""):
            dm = _in_facility(DeviceModel, data["device_model"])
            if dm is None:
                raise DRFValidationError({"device_model": ["Choose a device model from this facility."]})
        with transaction.atomic():
            procedure = _via_service(procedures.create_procedure, **fields, by=request.user)
            if dm is not None:
                _via_service(procedures.set_model_procedure, dm, procedure, by=request.user)
        return Response(self.get_serializer(self._fresh(procedure)).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, partial=False, **kwargs):
        """update_procedure: PUT sends every field (Edit procedure's form), PATCH only those to change."""
        procedure = self.get_object()
        data = _body(request)
        _check_fields(data, writable=self.WRITABLE, read_only=s.PROCEDURE_READ_ONLY, shown=self.get_serializer(procedure).data, hints=self.HINTS)
        fields = self._fields(data, every=not partial)
        before = {f: getattr(procedure, f) for f in procedures.EDITABLE}
        _via_service(procedures.update_procedure, procedure, by=request.user, **fields)
        changed = [f for f in procedures.EDITABLE if getattr(procedure, f) != before[f]]
        return Response({**self.get_serializer(self._fresh(procedure)).data, "changed": changed})


# --- a device model's PM program ------------------------------------------------------------------------------------------------


class DeviceModelProgramViewSet(PmProgramAccess, ApiViewSet):
    """The model drawer's tabs, nested under the device model: its procedure, its risk score, and its AEM program. Reads at PM View
    (the drawer's door); writes at the level each needs (write_level, and the checks in the actions)."""

    module = "pm"
    # What ModulePermission checks for each write. The risk score is equipment data: the drawer's PM View here, and Equipment
    # Approve in the action (eq_perms.can_set_risk), as the risk modal does it.
    WRITE_LEVELS = {"set_procedure": pm_perms.PROGRAM_EDIT_LEVEL, "set_risk_score": pm_perms.VIEW_LEVEL, "clear_risk_score": pm_perms.VIEW_LEVEL,
                    "propose_aem": pm_perms.PROGRAM_EDIT_LEVEL, "end_aem": pm_perms.AEM_DECIDE_LEVEL}
    refusals = {"set_procedure": CHOOSE_REFUSAL, "propose_aem": PROPOSE_REFUSAL, "end_aem": END_REFUSAL}

    @property
    def write_level(self):
        return self.WRITE_LEVELS.get(self.action, pm_perms.AEM_DECIDE_LEVEL)

    @property
    def delete_level(self):
        return self.WRITE_LEVELS.get(self.action, pm_perms.AEM_DECIDE_LEVEL)

    def _model(self, pk) -> DeviceModel:
        """Tenant-scoped: another facility's model, or anything that is not an id, is a 404."""
        return get_object_or_404(DeviceModel.objects.select_related("pm_procedure"), pk=pk)

    # The Procedure tab ----------------------------------------------------------------------------------------------------------

    def _procedure_data(self, dm) -> dict:
        p = dm.pm_procedure
        return {"device_model": str(dm.pk), "procedure": str(p.pk) if p else None,
                "procedure_detail": s.ProcedureSerializer(p).data if p else None,
                "hours": _hours(pm_schedule.pm_hours(dm)), "default_hours": _hours(pm_schedule.DEFAULT_PM_HOURS)}

    @action(detail=False, methods=["get"])
    def procedure(self, request, model_pk=None):
        return Response(self._procedure_data(self._model(model_pk)))

    @procedure.mapping.put
    def set_procedure(self, request, model_pk=None):
        """As the Procedure tab's chooser (views_procedures.model_procedure): set_model_procedure, or nothing when it is the same."""
        _require(pm_perms.can_edit_program(request.user), CHOOSE_REFUSAL)
        dm = self._model(model_pk)
        data = _body(request)
        shown = self._procedure_data(dm)
        _check_fields(data, writable=("procedure",), read_only=tuple(k for k in shown if k != "procedure"), shown=shown)
        if "procedure" not in data:
            raise DRFValidationError({"procedure": ["Required: a procedure's id, or null for none."]})
        value = data["procedure"]
        procedure = None
        if value not in (None, ""):
            procedure = _in_facility(PmProcedure, value)
            if procedure is None:
                raise DRFValidationError({"procedure": ["Choose a procedure from this facility."]})
        changed = procedure != dm.pm_procedure
        if changed:
            _via_service(procedures.set_model_procedure, dm, procedure, by=request.user)
        return Response({**self._procedure_data(self._model(model_pk)), "changed": changed})

    # The risk score ---------------------------------------------------------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="risk-score")
    def risk_score(self, request, model_pk=None):
        return Response(s.risk_score_data(self._model(model_pk), _today()))

    @risk_score.mapping.put
    def set_risk_score(self, request, model_pk=None):
        """As the risk modal (views_models.model_risk): Equipment Approve, then set_risk_score with the four parts."""
        _require(eq_perms.can_set_risk(request.user), RISK_REFUSAL)
        dm = self._model(model_pk)
        data = _body(request)
        shown = s.risk_score_data(dm, _today())
        _check_fields(data, writable=s.RISK_KEYS, read_only=tuple(k for k in shown if k not in s.RISK_KEYS), shown=shown)
        before = {"risk_class": dm.risk_class, "parts": tuple(getattr(dm, f) for _k, f, *_r in eq_services.RISK_PARTS)}
        _via_service(eq_services.set_risk_score, dm, by=request.user, **{key: data.get(key) for key in s.RISK_KEYS})
        parts = tuple(getattr(dm, f) for _k, f, *_r in eq_services.RISK_PARTS)
        effects = {"previous_class": before["risk_class"], "class_changed": dm.risk_class != before["risk_class"],
                   "review": parts == before["parts"], "aem": s.aem_effect_data(getattr(dm, "aem_effect", None))}
        return Response({**s.risk_score_data(dm, _today()), "effects": effects})

    @risk_score.mapping.delete
    def clear_risk_score(self, request, model_pk=None):
        """The risk modal's Clear: back to unscored, the class as it is."""
        _require(eq_perms.can_set_risk(request.user), RISK_REFUSAL)
        dm = self._model(model_pk)
        if dm.risk_score is None:
            raise DRFValidationError({"detail": f"{dm} has no risk score to clear."})
        eq_services.clear_risk_score(dm, by=request.user)
        return Response(s.risk_score_data(dm, _today()))

    # The AEM tab ------------------------------------------------------------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="aem", url_name="aem")
    def aem_program(self, request, model_pk=None):
        return Response(s.aem_data(self._model(model_pk), _today()))

    @aem_program.mapping.post
    def propose_aem(self, request, model_pk=None):
        """As Propose (views_aem.aem_propose): aem.propose with the interval and the case for it."""
        _require(pm_perms.can_edit_program(request.user), PROPOSE_REFUSAL)
        dm = self._model(model_pk)
        data = _body(request)
        _check_fields(data, writable=("interval_months", "rationale"))
        decision = _via_service(aem.propose, dm, interval_months=data.get("interval_months"), rationale=_text(data, "rationale"), by=request.user)
        return Response(s.AemDecisionSerializer(decision).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="aem/end", url_name="aem-end")
    def end_aem(self, request, model_pk=None):
        """As End AEM (views_aem.aem_end, by the model): the interval in force, approved or on file without a recorded approval."""
        _require(pm_perms.can_decide_aem(request.user), END_REFUSAL)
        dm = self._model(model_pk)
        data = _body(request)
        _check_fields(data, writable=("reason",))
        moved = _via_service(aem.end, dm, by=request.user, reason=_text(data, "reason"))
        return Response({**s.aem_data(self._model(model_pk), _today()), "devices_moved": moved})


# --- AEM decisions --------------------------------------------------------------------------------------------------------------


class AemDecisionViewSet(PmProgramAccess, TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """Every AEM case in the facility, and the committee's decision on an open one (apps.pm.aem)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "pm"
    scoped_actions = frozenset()
    serializer_class = s.AemDecisionSerializer
    ordering_fields = ["proposed_on", "decided_on", "interval_months"]
    WRITE_LEVELS = {"approve": pm_perms.AEM_DECIDE_LEVEL, "reject": pm_perms.AEM_DECIDE_LEVEL, "withdraw": pm_perms.PROGRAM_EDIT_LEVEL}
    refusals = {"approve": DECIDE_REFUSAL, "reject": DECIDE_REFUSAL, "withdraw": WITHDRAW_REFUSAL}

    @property
    def write_level(self):
        return self.WRITE_LEVELS.get(self.action, pm_perms.AEM_DECIDE_LEVEL)

    def get_queryset(self):
        qs = AemDecision.objects.select_related("device_model", "proposed_by", "decided_by", "ended_by")
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("device_model"):
            pk = _uuid(params["device_model"])
            if pk is None:
                raise DRFValidationError({"device_model": ["Not a device model's id."]})
            qs = qs.filter(device_model_id=pk)
        if params.get("status"):
            if params["status"] not in AemStatus.values:
                raise DRFValidationError({"status": [f"One of {', '.join(AemStatus.values)}."]})
            qs = qs.filter(status=params["status"])
        return qs

    def _decision_body(self, request, decision):
        """The committee's decision (views_aem.aem_decide): PM Approve, and never the proposer's own case (403, in
        aem.decide_blocker's words); the meeting date and the minutes reference."""
        _require(pm_perms.can_decide_aem(request.user), DECIDE_REFUSAL)
        if decision.status == AemStatus.PROPOSED and decision.proposed_by_id and decision.proposed_by_id == request.user.pk:
            raise PermissionDenied(aem.decide_blocker(decision, request.user))
        data = _body(request)
        _check_fields(data, writable=("decided_on", "note"))
        raw = data.get("decided_on")
        decided_on = None
        if raw not in (None, ""):
            decided_on = _parse_day(raw)
            if decided_on is None:
                raise DRFValidationError({"decided_on": ["Enter the committee's meeting date as YYYY-MM-DD."]})
        return {"by": request.user, "decided_on": decided_on, "note": _text(data, "note")}

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        decision = self.get_object()
        approved = _via_service(aem.approve, decision, **self._decision_body(request, decision))
        return Response({**self.get_serializer(self.get_object()).data, "devices_moved": approved.devices_moved})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        decision = self.get_object()
        _via_service(aem.reject, decision, **self._decision_body(request, decision))
        return Response(self.get_serializer(self.get_object()).data)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        """As Withdraw (views_aem.aem_withdraw): the proposer while they can still work on the PM program, or a PM Approve holder."""
        decision = self.get_object()
        user = request.user
        _require(pm_perms.can_decide_aem(user) or (decision.proposed_by_id == user.pk and pm_perms.can_edit_program(user)), WITHDRAW_REFUSAL)
        _check_fields(_body(request), writable=())
        _via_service(aem.withdraw, decision, by=user)
        return Response(self.get_serializer(self.get_object()).data)


MODEL = r"device-models/(?P<model_pk>[^/.]+)"


def register(router):
    router.register("pm", PmViewSet, basename="pm")
    router.register("pm-procedures", PmProcedureViewSet, basename="pmprocedure")
    router.register("aem-decisions", AemDecisionViewSet, basename="aemdecision")
    router.register(MODEL, DeviceModelProgramViewSet, basename="devicemodel-program")
