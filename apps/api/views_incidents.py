"""
Device incidents (slice 28) over the API: /api/v1/incidents/ through apps.incidents.services, as the Incidents screen changes them
(apps/web/views_incidents.py), with its doors (apps.incidents.permissions), checked server-side on every request: Incidents View to
read, Edit to record (RECORD_LEVEL), Approve to decide (DECIDE_LEVEL), and the services' own checks on top (holding a device also
needs Equipment Edit, opening a work order Work orders Edit, returning a device to use Equipment Edit). Module.INCIDENTS, and no
scoped_actions: a vendor's or a requester's account is refused every endpoint (403), whatever its levels. An incident is named in the
address by its id or its number (IN-26-0004, in any letter case).

GET    incidents/                          Incidents View. Newest first (the day it happened), 50 a page, each without its holds'
                                           release_note. ?status= open | closed | in_error | all (the default); ?year= the year it
                                           happened; ?search= number, device tag, or report number; ?ordering= occurred_on, aware_on,
                                           report_due_on, number (a leading "-" reverses it).
GET    incidents/<id or number>/           Incidents View. One incident (serializers_incidents.IncidentSerializer): the facts, the
                                           clock, the decision, the reports, the finding, the investigation, and the holds, each with
                                           its release_note.
POST   incidents/                          Incidents Edit (record_incident). {"asset": a device's id or tag, "occurred_on",
                                           "aware_on" (YYYY-MM-DD; the day it happened defaults to today, or to the adopted request's
                                           day; the day clinical staff first knew to the day it happened), "outcome", "affected",
                                           "event_reference", "accessories", "event_log", "hold" (true, the default: the device is
                                           held as evidence and an investigation opened), "work_order" (an open repair on the device
                                           to adopt as the investigation, by number or id), "open_work_order" (with "hold" false:
                                           open an investigation anyway)}. 201 with the incident.
POST   incidents/<id>/facts/               Incidents Edit (update_facts). Any of occurred_on, aware_on, outcome, affected,
PATCH                                      event_reference, accessories, event_log, and "aware_reason" (an AwareChange value) when
                                           aware_on moves later; only what is sent changes. Lowering the outcome, moving aware_on
                                           later, or stopping the clock needs Incidents Approve (403).
POST   incidents/<id>/decide/              Incidents Approve (decide). {"outcome" (the final one), "basis", "decided_on",
                                           "decided_by"}, all required.
POST   incidents/<id>/reports/             Incidents Approve (record_reports). {"fda_reported_on", "manufacturer_reported_on",
                                           "report_number"}: what is left out keeps what is recorded; null clears a date.
POST   incidents/<id>/finding/             Incidents Edit (record_finding). {"finding"}. The answer carries decision_cleared (true when
                                           a failure found cleared a decision that the device was not suggested) and message (the
                                           words to show then, else "").
POST   incidents/<id>/hold/                Incidents Edit and Equipment Edit (hold_device). {"asset": a device's id or tag}: another part
                                           of the system held under the same incident (an incident with no investigation opens one:
                                           Work orders Edit too).
POST   incidents/<id>/open-investigation/  Incidents Edit and Work orders Edit (open_investigation). No body: when it has none, or its
                                           work order was cancelled.
A hold (<hold>: its id, or its device's tag in any letter case):
POST   incidents/<id>/holds/<hold>/sent/   Incidents Approve (sent_to_manufacturer). {"sent_on"}, required.
POST   incidents/<id>/holds/<hold>/back/   Incidents Approve (back_from_manufacturer). {"back_on"}, required.
POST   incidents/<id>/holds/<hold>/release/
                                           Incidents Approve (release; returning to use also Equipment Edit). {"release":
                                           return_to_use | keep_out | kept_by_manufacturer}. The answer carries release_note: what
                                           still keeps the device out of service, "" when nothing does.
POST   incidents/<id>/close/               Incidents Approve (close). No body. Everything missing is refused at once, keyed by field.
POST   incidents/<id>/reopen/              Incidents Approve (reopen). No body.
POST   incidents/<id>/in-error/            Incidents Approve (recorded_in_error). No body.

Every write answers with the incident as GET shows it. A service's refusal is a 400 keyed by field in its words (a refusal about
the whole incident, such as "closed: reopen it first", as detail), a field an endpoint does not take a 400 ("Unknown field."), a
date not written YYYY-MM-DD a 400 on that field, a device or work order not in this facility a 400 on that field, another facility's
incident or a hold of another incident a 404, a level missing a 403 in the services' words. Incidents are never deleted.
"""
import uuid

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.fields import BooleanField
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.equipment.models import Asset
from apps.incidents import permissions as perms
from apps.incidents import services as inc
from apps.incidents.models import Incident, IncidentHold, Status
from apps.workorders.models import WorkOrder

from . import serializers_incidents as si
from .base import _parse_day, _via_service
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

VIEW_REFUSAL = "Reading incidents needs Incidents View."
FORM_TOKEN = "csrfmiddlewaretoken"  # the browsable API's form posts it
STATUS_FILTERS = (*Status.values, "all")
MIN_YEAR = 1900  # a year filter before it is a typo (and year 0 is no date at all)


def _today():
    """The incidents endpoints' clock, in one place so tests can pin it: the facility's today (the API works in its time zone,
    apps.api.tenancy), read once per request and passed to the services and the clock."""
    return timezone.localdate()


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _body(request, allowed) -> dict:
    """The write's fields: a JSON object (or a form) holding only `allowed`; anything else is refused, never ignored."""
    data = request.data
    if not hasattr(data, "keys"):
        raise DRFValidationError({"detail": "Send the fields as a JSON object."})
    unknown = sorted(set(data.keys()) - set(allowed) - {FORM_TOKEN})
    if unknown:
        raise DRFValidationError({name: ["Unknown field."] for name in unknown})
    return {k: data.get(k) for k in data.keys() if k != FORM_TOKEN}


def _text(data, key) -> str:
    """A text field: "" when missing or null; anything but text is a 400 on that field (the services read text)."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DRFValidationError({key: ["Send this as text."]})
    return value


def _date(data, key):
    """A YYYY-MM-DD day, or None when missing, null, or blank (the service says whether it may be); anything else is a 400 on it."""
    value = data.get(key)
    if value in (None, ""):
        return None
    day = _parse_day(value)
    if day is None:
        raise DRFValidationError({key: ["Enter the day as YYYY-MM-DD."]})
    return day


def _flag(data, key, default: bool) -> bool:
    if data.get(key) is None:
        return default
    try:
        return BooleanField().to_internal_value(data[key])
    except DRFValidationError:
        raise DRFValidationError({key: ["Send true or false."]}) from None


def _asset(data, key="asset") -> Asset:
    """The device a body names, by id or tag (any letter case), in this facility."""
    value = data.get(key)
    if value in (None, ""):
        raise DRFValidationError({key: ["Required: the device's id or tag."]})
    if not isinstance(value, str):
        raise DRFValidationError({key: ["Send the device's id or tag as text."]})
    pk = _uuid(value)
    found = Asset.objects.filter(pk=pk).first() if pk else Asset.objects.filter(tag__iexact=value.strip()).first()
    if found is None:
        raise DRFValidationError({key: [f"No device {value.strip()} in this facility."]})
    return found


def _work_order(data, key="work_order"):
    """The work order a body names, by number (any letter case) or id, in this facility; None when not sent."""
    value = data.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DRFValidationError({key: ["Send the work order's number or id as text."]})
    pk = _uuid(value)
    found = WorkOrder.objects.filter(pk=pk).first() if pk else WorkOrder.objects.filter(number__iexact=value.strip()).first()
    if found is None:
        raise DRFValidationError({key: [f"No work order {value.strip()} in this facility."]})
    return found


class IncidentViewSet(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    """The incidents (the module docstring). Reads need Incidents View; each write the level in WRITE_LEVELS (ModulePermission's door,
    refused in REFUSALS' words), then the service's own checks (PermissionDenied: a 403 in its words)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = perms.MODULE
    scoped_actions = frozenset()  # never a scoped user's: incidents are not theirs (the device shows only the hold's words)
    serializer_class = si.IncidentSerializer
    search_fields = ["number", "asset__tag", "report_number"]
    ordering_fields = ["occurred_on", "aware_on", "report_due_on", "number"]
    WRITE_LEVELS = {"create": perms.RECORD_LEVEL, "facts": perms.RECORD_LEVEL, "finding": perms.RECORD_LEVEL, "hold": perms.RECORD_LEVEL,
                    "open_investigation": perms.RECORD_LEVEL, "decide": perms.DECIDE_LEVEL, "reports": perms.DECIDE_LEVEL,
                    "sent": perms.DECIDE_LEVEL, "back": perms.DECIDE_LEVEL, "release": perms.DECIDE_LEVEL, "close": perms.DECIDE_LEVEL,
                    "reopen": perms.DECIDE_LEVEL, "in_error": perms.DECIDE_LEVEL}
    REFUSALS = {"create": inc.RECORD_PERMISSION, "facts": inc.FACTS_PERMISSION, "finding": inc.FINDING_PERMISSION, "hold": inc.HOLD_PERMISSION,
                "open_investigation": inc.OPEN_WORK_ORDER_PERMISSION, "decide": inc.DECIDE_PERMISSION, "reports": inc.REPORTS_PERMISSION,
                "sent": inc.CUSTODY_PERMISSION, "back": inc.CUSTODY_PERMISSION, "release": inc.RELEASE_PERMISSION,
                "close": inc.CLOSE_PERMISSION, "reopen": inc.CLOSE_PERMISSION, "in_error": inc.IN_ERROR_PERMISSION}

    @property
    def write_level(self):
        return self.WRITE_LEVELS.get(self.action, perms.DECIDE_LEVEL)

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in the services' words; the scoped refusal keeps its own."""
        if message is None and request.user and request.user.is_authenticated:
            message = self.REFUSALS.get(getattr(self, "action", None), VIEW_REFUSAL)
        super().permission_denied(request, message=message, code=code)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.today = _today()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["today"] = getattr(self, "today", None) or _today()
        context["notes"] = self.action != "list"  # a hold's release_note reads the device again: one incident at a time
        return context

    def get_queryset(self):
        holds = IncidentHold.objects.select_related("asset", "released_by").order_by("held_on", "created_at")
        qs = (Incident.objects.select_related("asset__device_model", "work_order", "recorded_by", "closed_by", "created_by")
              .prefetch_related(Prefetch("holds", queryset=holds)))
        if self.action != "list":
            return qs
        params = self.request.query_params
        wanted = params.get("status") or "all"
        if wanted not in STATUS_FILTERS:
            raise DRFValidationError({"status": [f"One of {', '.join(STATUS_FILTERS)}."]})
        if wanted != "all":
            qs = qs.filter(status=wanted)
        year = params.get("year")
        if year not in (None, ""):
            if not (year.isascii() and year.isdigit() and len(year) == 4 and int(year) >= MIN_YEAR):
                raise DRFValidationError({"year": ["A year, such as 2026."]})
            qs = qs.filter(occurred_on__year=int(year))
        return qs

    def get_object(self):
        """By id, or by number in any letter case (numbers are unique in a facility): another facility's is a 404."""
        value = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        pk = _uuid(value)
        qs = self.get_queryset()
        found = qs.filter(pk=pk).first() if pk else qs.filter(number__iexact=value).first()
        if found is None:
            raise NotFound("No incident with that id or number in this facility.")
        self.check_object_permissions(self.request, found)
        return found

    def _hold(self, incident, hold) -> IncidentHold:
        """One of this incident's holds, by its id or its device's tag: anything else is a 404."""
        pk = _uuid(hold)
        found = IncidentHold.objects.filter(incident=incident).filter(**({"pk": pk} if pk else {"asset__tag__iexact": hold})).first()
        if found is None:
            raise NotFound(f"{incident.number} has no hold {hold}.")
        return found

    def _shown(self, incident, code=status.HTTP_200_OK, **extra):
        """The incident as GET shows it, read again after the write (its holds and work order as they are now)."""
        fresh = self.get_queryset().get(pk=incident.pk)
        return Response({**self.get_serializer(fresh).data, **extra}, status=code)

    # --- recording ---------------------------------------------------------------------------------------------------------------

    CREATE_FIELDS = ("asset", "occurred_on", "aware_on", "outcome", "affected", "event_reference", "accessories", "event_log", "hold",
                     "work_order", "open_work_order")

    def create(self, request, *args, **kwargs):
        data = _body(request, self.CREATE_FIELDS)
        asset, work_order = _asset(data), _work_order(data)
        hold = _flag(data, "hold", True)
        open_work_order = _flag(data, "open_work_order", False)
        if work_order is not None and open_work_order:
            raise DRFValidationError({"open_work_order": ["Send work_order to adopt an open repair as the investigation, or open_work_order "
                                                          "to open one, not both."]})
        incident = _via_service(inc.record_incident, asset=asset, occurred_on=_date(data, "occurred_on"), aware_on=_date(data, "aware_on"),
                                outcome=_text(data, "outcome"), affected=_text(data, "affected"),
                                event_reference=_text(data, "event_reference"), hold=hold, work_order=work_order,
                                open_work_order=open_work_order, accessories=_text(data, "accessories"), event_log=_text(data, "event_log"),
                                by=request.user, today=self.today)
        return self._shown(incident, status.HTTP_201_CREATED)

    DATE_FACTS = ("occurred_on", "aware_on")

    @action(detail=True, methods=["post", "patch"])
    def facts(self, request, pk=None):
        incident = self.get_object()
        data = _body(request, (*inc.FACT_FIELDS, "aware_reason"))
        fields = {f: (_date(data, f) if f in self.DATE_FACTS else _text(data, f)) for f in inc.FACT_FIELDS if f in data}
        _via_service(inc.update_facts, incident, by=request.user, today=self.today, aware_reason=_text(data, "aware_reason"), **fields)
        return self._shown(incident)

    @action(detail=True, methods=["post"])
    def finding(self, request, pk=None):
        incident = self.get_object()
        data = _body(request, ("finding",))
        result = _via_service(inc.record_finding, incident, _text(data, "finding"), by=request.user)
        cleared = bool(getattr(result, "decision_cleared", False))
        return self._shown(incident, decision_cleared=cleared, message=inc.DECISION_CLEARED if cleared else "")

    @action(detail=True, methods=["post"])
    def hold(self, request, pk=None):
        incident = self.get_object()
        data = _body(request, ("asset",))
        _via_service(inc.hold_device, incident, _asset(data), by=request.user, today=self.today)
        return self._shown(incident)

    @action(detail=True, methods=["post"], url_path="open-investigation", url_name="open-investigation")
    def open_investigation(self, request, pk=None):
        incident = self.get_object()
        _body(request, ())
        _via_service(inc.open_investigation, incident, by=request.user, today=self.today)
        return self._shown(incident)

    # --- deciding and reporting --------------------------------------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        incident = self.get_object()
        data = _body(request, ("outcome", "basis", "decided_on", "decided_by"))
        _via_service(inc.decide, incident, outcome=_text(data, "outcome"), basis=_text(data, "basis"), decided_on=_date(data, "decided_on"),
                     decided_by=_text(data, "decided_by"), by=request.user, today=self.today)
        return self._shown(incident)

    REPORT_DATES = ("fda_reported_on", "manufacturer_reported_on")

    @action(detail=True, methods=["post"])
    def reports(self, request, pk=None):
        """What is left out keeps what is recorded (the service sets both dates as given); null or "" clears a date."""
        incident = self.get_object()
        data = _body(request, (*self.REPORT_DATES, "report_number"))
        kwargs = {f: (_date(data, f) if f in data else getattr(incident, f)) for f in self.REPORT_DATES}
        number = _text(data, "report_number") if "report_number" in data else incident.report_number
        _via_service(inc.record_reports, incident, report_number=number, by=request.user, today=self.today, **kwargs)
        return self._shown(incident)

    # --- the holds ---------------------------------------------------------------------------------------------------------------

    HOLD = r"holds/(?P<hold_pk>[^/]+)"

    @action(detail=True, methods=["post"], url_path=f"{HOLD}/sent", url_name="hold-sent")
    def sent(self, request, pk=None, hold_pk=None):
        incident = self.get_object()
        hold = self._hold(incident, hold_pk)
        data = _body(request, ("sent_on",))
        _via_service(inc.sent_to_manufacturer, hold, on=_date(data, "sent_on"), by=request.user)
        return self._shown(incident)

    @action(detail=True, methods=["post"], url_path=f"{HOLD}/back", url_name="hold-back")
    def back(self, request, pk=None, hold_pk=None):
        incident = self.get_object()
        hold = self._hold(incident, hold_pk)
        data = _body(request, ("back_on",))
        _via_service(inc.back_from_manufacturer, hold, on=_date(data, "back_on"), by=request.user)
        return self._shown(incident)

    @action(detail=True, methods=["post"], url_path=f"{HOLD}/release", url_name="hold-release")
    def release(self, request, pk=None, hold_pk=None):
        incident = self.get_object()
        hold = self._hold(incident, hold_pk)
        data = _body(request, ("release",))
        released = _via_service(inc.release, hold, _text(data, "release"), by=request.user, today=self.today)
        return self._shown(incident, release_note=inc.release_note(released))

    # --- closing -----------------------------------------------------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        incident = self.get_object()
        _body(request, ())
        _via_service(inc.close, incident, by=request.user, today=self.today)
        return self._shown(incident)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        incident = self.get_object()
        _body(request, ())
        _via_service(inc.reopen, incident, by=request.user)
        return self._shown(incident)

    @action(detail=True, methods=["post"], url_path="in-error", url_name="in-error")
    def in_error(self, request, pk=None):
        incident = self.get_object()
        _body(request, ())
        _via_service(inc.recorded_in_error, incident, by=request.user, today=self.today)
        return self._shown(incident)


def register(router) -> None:
    router.register("incidents", IncidentViewSet, basename="incident")
