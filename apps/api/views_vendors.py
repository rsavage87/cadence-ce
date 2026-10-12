"""
Vendor staff (slice 31): the vendors' engineers, their training on file, and the vendors' attestations, through apps.vendors.services
as the Contracts screen's Vendor staff tab changes them (apps/web/views_vendors.py). Cadence keeps the documents and never judges
competence: a training or an attestation is "on file", never "trained" or "qualified" (apps.vendors.rules).

Levels, on the Contracts module (apps.vendors.permissions): View reads, Edit adds, changes, and removes. No scoped_actions: a vendor's
or a requester's account is refused every endpoint (403), whatever its levels, even with Contracts on a custom role. Another
facility's record in the address is a 404, and in a body (an engineer, a device model) a 400, as an unknown id. A service's refusal is
a 400 keyed by field in its words; a field an endpoint does not take a 400 ("Unknown field."); a day not written YYYY-MM-DD a 400 on
that field; text sent as a list, number, or object a 400 on that field.

Engineers (never deleted: deactivated; their company is fixed once a work order names them, a move to another vendor is a new engineer)
  GET    /api/v1/vendor-engineers/        Contracts View. By company, then name. Each: id, name, company (the vendor, as work orders and
                                          contracts name it), is_active, trainings (each as below). ?vendor=<a vendor's name> (any
                                          spelling of it: "PHILIPS field service" is Philips, rules.vendor_key), ?active=true|false,
                                          ?search= (name or company), ?ordering= name, company.
  GET    /api/v1/vendor-engineers/{id}/   Contracts View.
  POST   /api/v1/vendor-engineers/        Contracts Edit. {"name" (letters, spaces, and . ' - , only), "company"}: add_engineer. 201.
                                          The same name at the same vendor (any letter case, or "Last, First") is a 400 keyed name.
  PATCH  /api/v1/vendor-engineers/{id}/   Contracts Edit. Any of {"name", "company", "is_active"}: update_engineer (an inactive
                                          engineer is never offered for new work; their records stay). A field the GET shows may be
                                          sent back as it reads. PUT and DELETE are not offered.

Trainings on file (one engineer's certificate or training record; a renewal is a new record)
  GET    /api/v1/vendor-trainings/        Contracts View. Newest issued first. Each: id, engineer (its id), engineer_name, company,
                                          covers ("models" or "line"), covers_label, model_ids, manufacturer, category, coverage (in
                                          words), source (oem, vendor, third_party), source_label, certificate, issued_on,
                                          expires_on (null: no expiry), in_force (today), words ("Training on file: ..."), entered_on
                                          (the day it was entered in Cadence). ?engineer=<id>, ?vendor=<a vendor's name>.
  GET    /api/v1/vendor-trainings/{id}/   Contracts View.
  POST   /api/v1/vendor-trainings/        Contracts Edit. add_training: {"engineer": <id>} for one engineer (201 with the training), or
                                          {"engineers": [<id>, ...]} for several (a class the vendor ran: 201 with a list, one training
                                          each), with "covers": "models" and "model_ids": [<device model id>, ...] (catalog models), or
                                          "covers": "line" with "manufacturer" and "category" (one of the catalog's product categories:
                                          never a whole manufacturer); "source" (oem, vendor, third_party), "certificate" (optional, one
                                          engineer only), "issued_on" (never in the future), "expires_on" (optional).
  DELETE /api/v1/vendor-trainings/{id}/   Contracts Edit. remove_training: entered in error, so it covers no day (its history keeps it).
                                          204. A training that lapsed stays on file: it covered its own days.

Attestations on file (a vendor's written statement that everyone who services these devices for the facility is trained on them)
  GET    /api/v1/vendor-attestations/     Contracts View. The one running longest first. Each: id, vendor_names, reference,
                                          received_on, valid_from, valid_through, covers ("all", "models", or "line"), covers_label,
                                          model_ids, manufacturer, category, coverage, in_force (today), entered_on. ?vendor=<a vendor's
                                          name> (those naming it, any spelling), ?search= reference, ?ordering= valid_through,
                                          valid_from, received_on, reference.
  GET    /api/v1/vendor-attestations/{id}/
                                          Contracts View.
  POST   /api/v1/vendor-attestations/     Contracts Edit. add_attestation: {"vendor_names": [the vendor's names as work orders and
                                          contracts write them, e.g. "Philips Healthcare", "Philips field service"], "covers" ("all":
                                          every device this vendor services for us; "models" with "model_ids"; "line" with
                                          "manufacturer" and "category"), "reference" (the letter's, or the contract and clause, without
                                          spaces), "received_on" (never in the future), "valid_from", "valid_through"} (all required:
                                          the period the letter states). 201.
  DELETE /api/v1/vendor-attestations/{id}/
                                          Contracts Edit. remove_attestation: entered in error. 204.
A training or an attestation is never edited in place (no PUT or PATCH): remove one entered in error and add it again.

Naming the engineer on a work order is the work order's (apps/api/views.py: the transition to completed takes vendor_engineer or
engineer_name; POST /api/v1/work-orders/{id}/vendor-engineer/ afterwards; GET a work order for its engineer_choices).
"""
import uuid

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.fields import BooleanField
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.vendors import permissions as vendor_perms
from apps.vendors import rules, services
from apps.vendors.models import EngineerTraining, VendorAttestation, VendorEngineer

from . import serializers_vendors as sv
from .base import _parse_day, _via_service
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

FORM_TOKEN = "csrfmiddlewaretoken"  # the browsable API's form posts it
VIEW_REFUSAL = "Reading vendor staff records needs Contracts View."
EDIT_REFUSAL = "Adding, changing, or removing vendor staff records needs Contracts Edit."
AS_TEXT = "Send this as text."


def _today():
    """The endpoints' clock, in one place so tests can pin it: the facility's today (the API works in its time zone,
    apps.api.tenancy), read once per request and passed to the services and the serializers."""
    return timezone.localdate()


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _body(request, allowed, *, shown=None) -> dict:
    """The write's fields: a JSON object (or a form) holding only `allowed`. A field the GET shows (`shown`) may be sent back as it
    reads ("Read only." when changed); anything else is refused ("Unknown field."), never ignored."""
    data = request.data
    if not hasattr(data, "keys"):
        raise DRFValidationError({"detail": "Send the fields as a JSON object."})
    errors = {}
    for key in data.keys():
        if key in allowed or key == FORM_TOKEN:
            continue
        if shown is not None and key in shown:
            if data.get(key) != shown[key]:
                errors[key] = ["Read only."]
            continue
        errors[key] = ["Unknown field."]
    if errors:
        raise DRFValidationError(errors)
    return data


def _text(data, key) -> str:
    """A text field: "" when missing or null; anything but text is a 400 on that field (the services read text)."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DRFValidationError({key: [AS_TEXT]})
    return value


def _date(data, key):
    """A YYYY-MM-DD day, or None when missing, null, or blank (the service says whether it is required); else a 400 on it."""
    value = data.get(key)
    if value in (None, ""):
        return None
    day = _parse_day(value)
    if day is None:
        raise DRFValidationError({key: ["Enter the day as YYYY-MM-DD."]})
    return day


def _texts(data, key, what) -> list:
    """A list of text values (a form repeats the key; one value alone is a list of one); [] when missing or null."""
    values = data.getlist(key) if hasattr(data, "getlist") else data.get(key)
    if values in (None, ""):
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
        raise DRFValidationError({key: [f"Send a list of {what}."]})
    return list(values)


def _flag(data, key) -> bool:
    try:
        return BooleanField().to_internal_value(data.get(key))
    except DRFValidationError:
        raise DRFValidationError({key: ["Send true or false."]}) from None


def _vendor_param(request):
    """?vendor=<a vendor's name>: its key (rules.vendor_key), or None when not given or blank."""
    key = rules.vendor_key(request.query_params.get("vendor", ""))
    return key or None


class VendorStaffViewSet(TenantAPIMixin, viewsets.GenericViewSet):
    """The three areas' shared doors: Contracts View reads, Edit writes and removes; never a scoped user (no scoped_actions)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = vendor_perms.MODULE
    write_level = delete_level = vendor_perms.EDIT_LEVEL  # removing a record entered in error is upkeep, as adding it
    scoped_actions = frozenset()

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in words; the scoped refusal keeps its own."""
        if message is None and request.user and request.user.is_authenticated:
            message = VIEW_REFUSAL if request.method in ("GET", "HEAD", "OPTIONS") else EDIT_REFUSAL
        super().permission_denied(request, message=message, code=code)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.today = _today()

    def _records(self, items) -> list:
        """The trainings and attestations whose coverage a page shows (their catalog models read in one go)."""
        return list(items)

    def _show(self, items, *, many=False):
        items = list(items)
        context = {**self.get_serializer_context(), "today": self.today, "models_by_id": sv.models_by_id(self._records(items))}
        data = self.get_serializer_class()(items, many=True, context=context).data
        return data if many else data[0]

    def list(self, request):
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        if page is None:
            return Response(self._show(qs, many=True))
        return self.get_paginated_response(self._show(page, many=True))

    def retrieve(self, request, pk=None):
        return Response(self._show([self.get_object()]))


# --- engineers -----------------------------------------------------------------------------------------------------------------

class VendorEngineerViewSet(VendorStaffViewSet):
    serializer_class = sv.EngineerSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]  # never deleted (work orders name them): deactivated
    search_fields = ["name", "company"]
    ordering_fields = ["name", "company"]
    WRITABLE = ("name", "company", "is_active")

    def get_queryset(self):
        qs = VendorEngineer.objects.prefetch_related(Prefetch("trainings", queryset=EngineerTraining.objects.select_related("engineer")))
        if self.action != "list":
            return qs
        key = _vendor_param(self.request)
        if key:
            qs = qs.filter(company_key=key)
        if self.request.query_params.get("active") not in (None, ""):
            qs = qs.filter(is_active=_flag(self.request.query_params, "active"))
        return qs

    def _records(self, items):
        return [t for e in items for t in e.trainings.all()]

    def create(self, request):
        body = _body(request, ("name", "company"))
        engineer = _via_service(services.add_engineer, name=_text(body, "name"), company=_text(body, "company"), by=request.user)
        return Response(self._show([self.get_queryset().get(pk=engineer.pk)]), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        engineer = self.get_object()
        body = _body(request, self.WRITABLE, shown=self._show([engineer]))
        changes = {f: _text(body, f) for f in ("name", "company") if f in body}
        if "is_active" in body:
            changes["is_active"] = _flag(body, "is_active")
        _via_service(services.update_engineer, engineer, by=request.user, **changes)
        return Response(self._show([self.get_queryset().get(pk=engineer.pk)]))


# --- trainings -----------------------------------------------------------------------------------------------------------------

class VendorTrainingViewSet(VendorStaffViewSet):
    serializer_class = sv.TrainingSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]  # a renewal is a new record; an error is removed
    search_fields = ["engineer__name", "engineer__company", "certificate"]
    ordering_fields = ["issued_on", "expires_on"]
    ALLOWED = ("engineer", "engineers", "covers", "model_ids", "manufacturer", "category", "source", "certificate", "issued_on", "expires_on")

    def get_queryset(self):
        qs = EngineerTraining.objects.select_related("engineer")
        if self.action != "list":
            return qs
        raw = self.request.query_params.get("engineer")
        if raw not in (None, ""):
            pk = _uuid(raw)
            if pk is None:
                raise DRFValidationError({"engineer": ["An engineer's id."]})
            qs = qs.filter(engineer_id=pk)
        key = _vendor_param(self.request)
        if key:
            qs = qs.filter(engineer__company_key=key)
        return qs

    @staticmethod
    def _engineers(body) -> tuple[list, bool]:
        """The engineers a body names (`engineer`: one id, or `engineers`: a list of ids), each once, in this facility; and whether one
        was sent alone (the answer is then the training, not a list)."""
        one = body.get("engineer")
        many = _texts(body, "engineers", "engineers' ids")
        if one not in (None, "") and many:
            raise DRFValidationError({"engineers": ["Send engineer (one id) or engineers (a list of ids), not both."]})
        if one not in (None, ""):
            if not isinstance(one, str):
                raise DRFValidationError({"engineer": ["Send the engineer's id as text."]})
            found = VendorEngineer.objects.filter(pk=_uuid(one)).first() if _uuid(one) else None
            if found is None:
                raise DRFValidationError({"engineer": ["Choose an engineer on file here."]})
            return [found], True
        wanted = list(dict.fromkeys(many))
        parsed = {raw: _uuid(raw) for raw in wanted}
        found = {str(e.pk): e for e in VendorEngineer.objects.filter(pk__in=[p for p in parsed.values() if p])}
        missing = [raw for raw in wanted if parsed[raw] is None or str(parsed[raw]) not in found]
        if missing:
            raise DRFValidationError({"engineers": [f"Not an engineer on file here: {', '.join(missing)}."]})
        return list(dict.fromkeys(found[str(parsed[raw])] for raw in wanted)), False

    def create(self, request):
        body = _body(request, self.ALLOWED)
        engineers, alone = self._engineers(body)
        made = _via_service(services.add_training, engineers, covers=_text(body, "covers"), model_ids=_texts(body, "model_ids", "device model ids"),
                            manufacturer=_text(body, "manufacturer"), category=_text(body, "category"), source=_text(body, "source"),
                            certificate=_text(body, "certificate"), issued_on=_date(body, "issued_on"), expires_on=_date(body, "expires_on"),
                            by=request.user, today=self.today)
        data = self._show(EngineerTraining.objects.select_related("engineer").filter(pk__in=[t.pk for t in made]).order_by("engineer__name"),
                          many=True)
        return Response(data[0] if alone else data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        services.remove_training(self.get_object(), by=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- attestations --------------------------------------------------------------------------------------------------------------

class VendorAttestationViewSet(VendorStaffViewSet):
    serializer_class = sv.AttestationSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]  # a renewal is a new record; an error is removed
    search_fields = ["reference"]
    ordering_fields = ["valid_through", "valid_from", "received_on", "reference"]
    ALLOWED = ("vendor_names", "covers", "model_ids", "manufacturer", "category", "reference", "received_on", "valid_from", "valid_through")

    def get_queryset(self):
        return VendorAttestation.objects.all()

    def filter_queryset(self, queryset):
        """?vendor= reads the stored keys in Python (a JSON list: SQLite cannot look inside one), after search and ordering."""
        qs = super().filter_queryset(queryset)
        key = _vendor_param(self.request) if self.action == "list" else None
        return [a for a in qs if key in (a.vendor_keys or ())] if key else qs

    def create(self, request):
        body = _body(request, self.ALLOWED)
        made = _via_service(services.add_attestation, vendor_names=_texts(body, "vendor_names", "the vendor's names"), covers=_text(body, "covers"),
                            model_ids=_texts(body, "model_ids", "device model ids"), manufacturer=_text(body, "manufacturer"),
                            category=_text(body, "category"), reference=_text(body, "reference"), received_on=_date(body, "received_on"),
                            valid_from=_date(body, "valid_from"), valid_through=_date(body, "valid_through"), by=request.user, today=self.today)
        return Response(self._show([made]), status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        services.remove_attestation(self.get_object(), by=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


def register(router):
    router.register("vendor-engineers", VendorEngineerViewSet, basename="vendor-engineer")
    router.register("vendor-trainings", VendorTrainingViewSet, basename="vendor-training")
    router.register("vendor-attestations", VendorAttestationViewSet, basename="vendor-attestation")
