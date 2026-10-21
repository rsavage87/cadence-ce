"""
/api/v1/ for the device security register (slice 37), read only: recording a model's releases and a device's facts is the Device security
screen's and the device drawer's Security tab's, through apps.cybersecurity.services. TenantAPIMixin, Module.CYBERSECURITY
(cybersecurity.permissions: the screen's doors), checked server-side on every request; no scoped_actions, so a vendor's or a requester's
account is refused every endpoint (403), whatever its levels. Every word is apps.cybersecurity.rules' (serializers_cybersecurity), judged
on the facility's today.

GET software-releases/              Device security View (model-level: no device is named). Each model's profile and versioned releases,
                                    by manufacturer, model, and version, 50 a page (serializers_cybersecurity.release_data): id,
                                    device_model {id, manufacturer, model, description, category, url}, version ("" for the profile),
                                    profile, groups {support, mds2, disclosure, facility}, each with its source and its fields (choices with
                                    a "_label"), the two standing lines security and os ({key, words, day}), devices (the devices on site
                                    recorded on it; for a profile, its model's devices on site with no release recorded), removed_on and
                                    removed_by. Releases entered in error are left out unless ?removed=1 (then read as recorded, with no
                                    standing). ?device_model= a model's id; ?search= manufacturer, model, or version. In a fixed number of
                                    queries for a page.
GET software-releases/<id>/         One of them (entered in error ones too).
GET device-security/                Device security View AND Equipment View (it names devices by tag and location). Every device on site
                                    (not retired: ours and temporary alike), by tag, 50 a page (serializers_cybersecurity.device_data): id,
                                    tag, device_model, department, room, ownership, release {id, version} or null, release_since,
                                    release_words, network (the device's own fact; "" as the model discloses) and its label, connection
                                    {key, words, here}, security and os, groups (the release over the profile, by group), checked_on,
                                    checked_by, and its Security tab on the web naming the facility (url). ?device_model= a model's id;
                                    ?search= a tag or serial; and the Fleet's filters, read by the screen's own parser
                                    (apps.cybersecurity.filters), so a head figure's link asks the same: ?whose= ours | temporary;
                                    ?department= (or the screen's ?dept=) a department's id; ?category= a model's category, as written;
                                    ?connected= either (connected here or able to connect, what the head figures count) | connected |
                                    can_connect | not_connected | not_recorded; ?standing= updates_ended | updates_end_announced |
                                    security_not_recorded | os_ended | os_end_announced; ?norelease=1 (no release recorded). An unknown
                                    value is a 400 naming the parameter. Through rules.standings: a fixed number of queries.
GET device-security/<id or tag>/    One device on site, by id or by tag in any letter case.
GET software-updates/               Slice 38. Device security View AND Equipment View (it names devices). Every update work order's
                                    record and every install recorded by hand (one entered in error is left out), newest first as the
                                    device's Security tab lists them (by the record's day: the day it was applied, else its work order's
                                    completion, else the day it was opened), 50 a page (serializers_cybersecurity.update_data): id,
                                    device {id, tag}, work_order {id, number} (null for one recorded by hand; never a link), target,
                                    target_release {id, version}, target_reference, state (open, done, not_confirmed, cancelled) and
                                    state_label, outcome and outcome_label, words (apps.cybersecurity.rules.update_words, the sentence
                                    the tab shows), applied_on, release_after {id, version}, reference, basis and
                                    basis_label, installed_by and installed_by_label, recorded_by (a name), later_kept, opened_on.
                                    ?device= a device's id, or its tag in any letter case (a retired one's too); ?release= a release's id
                                    (the release an update was to install, or the one the device runs after it); ?state= one of open,
                                    done, not_confirmed, cancelled. An unknown value is a 400 naming the parameter. One query for a page
                                    (and its count).
GET software-updates/<id>/          One of them, by id.

Another facility's row is a 404 (as an unknown one), a retired device too (but on software-updates/, which keeps a retired device's
updates); a level missing a 403 in plain words; any write a 405 naming the Device security screen (with Device security Edit) or a 403
(without it). What an update work order did is recorded by its transition to completed (POST work-orders/{id}/transition/ with
software_outcome, software_release (installed and already only; never with patches), software_reference, software_basis: the work
order's software_choices says what this user may send);
the work order itself reads it in software_update (serializers.WorkOrderSerializer), behind the same two levels.
"""
import uuid

from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import MethodNotAllowed, NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.filters import SearchFilter
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Level, Module
from apps.cybersecurity import filters, rules
from apps.cybersecurity import permissions as perms
from apps.cybersecurity.models import RECORDED_OUTCOMES, DeviceSecurity, SoftwareRelease, SoftwareUpdate
from apps.equipment.models import Asset, AssetStatus
from apps.workorders.models import OPEN_STATUSES, WoStatus

from . import serializers_cybersecurity as scs
from .permissions import ModulePermission
from .tenancy import TenantAPIMixin

RELEASES_REFUSAL = "Reading software releases needs Device security View."
DEVICES_REFUSAL = "Reading devices' security facts needs Device security View and Equipment View."
WRITE_REFUSAL = ("The API reads the device security register only. Recording a model's releases and a device's facts is done on the "
                 "Device security screen and the device's Security tab, and needs Device security Edit.")
READ_ONLY = ("The API reads the device security register only: record a model's releases and a device's facts on the Device security "
             "screen.")
RELEASE_NOT_FOUND = "No software release with that id in this facility."
DEVICE_NOT_FOUND = "No device on site with that id or tag in this facility."
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _id_filter(params, name: str, words: str, errors: dict):
    """A ?<name>= id: its UUID, None when not asked; a malformed one noted in `errors`."""
    raw = params.get(name) or ""
    if not raw:
        return None
    found = _uuid(raw)
    if found is None:
        errors[name] = [words]
    return found


class _ReadOnly(TenantAPIMixin, viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, ModulePermission]
    module = perms.MODULE
    delete_level = perms.EDIT_LEVEL  # a DELETE is a write like any other: Edit gets the 405, never a 403 naming a level held
    scoped_actions = frozenset()  # never a scoped user's, whatever their levels
    filter_backends = [SearchFilter]
    view_refusal = RELEASES_REFUSAL
    write_refusal = WRITE_REFUSAL

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in plain words (a write: Edit, the default write_level, kept so that a write added later is
        closed until its level is set); the scoped refusal and EquipmentView's keep their own. With the level, a write is a 405."""
        if message is None and request.user and request.user.is_authenticated:
            message = self.view_refusal if request.method in SAFE_METHODS else self.write_refusal
        super().permission_denied(request, message=message, code=code)

    def http_method_not_allowed(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=READ_ONLY)

    def _today(self):
        """The facility's today (its time zone is active: TenantAPIMixin), read once per request."""
        if not hasattr(self, "_today_value"):
            self._today_value = timezone.localdate()
        return self._today_value


class SoftwareReleaseViewSet(_ReadOnly):
    """The models' profiles and releases (the module docstring). Device security View."""

    serializer_class = scs.SoftwareReleaseSerializer
    search_fields = ["device_model__manufacturer", "device_model__model", "version"]

    def get_queryset(self):
        qs = SoftwareRelease.objects.select_related("device_model", "removed_by")
        if self.action == "list":
            params, errors = self.request.query_params, {}
            model = _id_filter(params, "device_model", "A device model's id.", errors)
            removed = params.get("removed") or ""
            if removed not in ("", "0", "1"):
                errors["removed"] = ["1 to list releases entered in error too."]
            if errors:
                raise DRFValidationError(errors)
            if model is not None:
                qs = qs.filter(device_model_id=model)
            if removed != "1":
                qs = qs.filter(removed_on__isnull=True)
        return qs.order_by("device_model__manufacturer", "device_model__model", "device_model_id", "version_key", "pk")

    def _context(self, rows) -> dict:
        """What the page's rows read: their models' profiles (one query), the devices on each versioned release (one), and for each
        profile its model's devices on site with no release recorded (two). Nothing per row."""
        model_ids = {r.device_model_id for r in rows}
        profiles = {p.device_model_id: p for p in SoftwareRelease.objects.filter(device_model_id__in=model_ids, version_key="",
                                                                                  removed_on__isnull=True)} if model_ids else {}
        on_site = DeviceSecurity.objects.exclude(asset__status=AssetStatus.RETIRED)
        devices = {}
        versioned = [r.pk for r in rows if not r.is_profile]
        if versioned:
            devices.update(on_site.filter(release_id__in=versioned, release__device_model_id=F("asset__device_model_id"))
                           .values("release_id").annotate(n=Count("pk")).values_list("release_id", "n"))
        profile_rows = [r for r in rows if r.is_profile]
        if profile_rows:
            ids = {r.device_model_id for r in profile_rows}
            totals = dict(Asset.objects.exclude(status=AssetStatus.RETIRED).filter(device_model_id__in=ids).values("device_model_id")
                          .annotate(n=Count("pk")).values_list("device_model_id", "n"))
            recorded = dict(on_site.filter(asset__device_model_id__in=ids, release__isnull=False, release__removed_on__isnull=True,
                                           release__device_model_id=F("asset__device_model_id"))
                            .values("asset__device_model_id").annotate(n=Count("pk")).values_list("asset__device_model_id", "n"))
            devices.update({r.pk: totals.get(r.device_model_id, 0) - recorded.get(r.device_model_id, 0) for r in profile_rows})
        return {**self.get_serializer_context(), "today": self._today(), "profiles": profiles, "devices": devices}

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        rows = list(page if page is not None else qs)
        data = scs.SoftwareReleaseSerializer(rows, many=True, context=self._context(rows)).data
        return self.get_paginated_response(data) if page is not None else Response(data)

    def get_object(self):
        pk = _uuid(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field))
        found = self.get_queryset().filter(pk=pk).first() if pk is not None else None
        if found is None:
            raise NotFound(RELEASE_NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found

    def retrieve(self, request, *args, **kwargs):
        release = self.get_object()
        return Response(scs.SoftwareReleaseSerializer(release, context=self._context([release])).data)


class EquipmentView(BasePermission):
    """The device lists' second door: Equipment View (permissions.can_view_devices), checked after ModulePermission, so a scoped user
    keeps its own refusal; refused in the view's own words (its view_refusal, or its write_refusal for a write)."""

    def has_permission(self, request, view):
        if request.user.has_level(Module.EQUIPMENT, Level.VIEW):
            return True
        self.message = (getattr(view, "view_refusal", DEVICES_REFUSAL) if request.method in SAFE_METHODS
                        else getattr(view, "write_refusal", WRITE_REFUSAL))
        return False


class DeviceSecurityViewSet(_ReadOnly):
    """Each device on site with its facts and standing words (the module docstring). Device security View and Equipment View."""

    permission_classes = [IsAuthenticated, ModulePermission, EquipmentView]
    serializer_class = scs.DeviceSecuritySerializer
    view_refusal = DEVICES_REFUSAL
    lookup_value_regex = "[^/]+"  # a tag may hold a dot
    search_fields = ["tag", "serial"]

    fleet_filters = filters.FleetFilters()

    def get_queryset(self):
        qs = rules.on_site()
        if self.action == "list":
            params, errors = self.request.query_params, {}
            model = _id_filter(params, "device_model", "A device model's id.", errors)
            try:
                self.fleet_filters = filters.parse_strict(params)
            except filters.FilterError as e:
                errors.update(e.errors)
            if errors:
                raise DRFValidationError(errors)
            if model is not None:
                qs = qs.filter(device_model_id=model)
            qs = filters.narrow(qs, self.fleet_filters)
        return qs.order_by("tag", "pk")

    def list(self, request, *args, **kwargs):
        """The devices with their standings (rules.standings: two queries for any number), then the Fleet's filters on a device's
        facts (connected, standing, norelease: FleetFilters.matches, as the screen), then the page."""
        found = list(rules.standings(self.filter_queryset(self.get_queryset()), self._today()).values())
        if self.fleet_filters.by_device:
            found = [s for s in found if self.fleet_filters.matches(s)]
        page = self.paginate_queryset(found)
        if page is not None:
            return self.get_paginated_response(scs.DeviceSecuritySerializer(page, many=True).data)
        return Response(scs.DeviceSecuritySerializer(found, many=True).data)

    def get_object(self):
        """By id, or by tag in any letter case, among the devices on site. Another facility's, or a retired one, is a 404."""
        value = str(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field) or "")
        qs = self.get_queryset()
        pk = _uuid(value)
        found = qs.filter(pk=pk).first() if pk is not None else None
        if found is None and value:
            found = qs.filter(tag__iexact=value).first()
        if found is None:
            raise NotFound(DEVICE_NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found

    def retrieve(self, request, *args, **kwargs):
        asset = self.get_object()
        return Response(scs.device_data(rules.standings([asset], self._today())[asset.pk]))


UPDATES_REFUSAL = "Reading software updates needs Device security View and Equipment View."
UPDATES_READ_ONLY = ("The API reads software updates only: open one, record an install, confirm a version, or mark one entered in error "
                     "on the Device security screen and the device's Security tab, and record what an update work order did by "
                     "completing it (POST /api/v1/work-orders/{id}/transition/ with software_outcome).")
UPDATES_WRITE_REFUSAL = ("The API reads software updates only. Opening and recording them is done on the Device security screen and the "
                         "device's Security tab, and needs Device security Edit.")
UPDATE_NOT_FOUND = "No software update with that id in this facility."
STATE_WORDS = f"One of {', '.join(rules.STATES)}."


def state_q(state: str) -> Q:
    """The rows in `state` (one of rules.STATES), positive conditions only, as rules.update_state reads a row (a test holds every row
    to it): open, a work order open, in progress, or awaiting parts; not_confirmed, completed or closed with nothing recorded; done, an
    install recorded by hand (one entered in error is never listed), or completed or closed with an outcome; cancelled."""
    finished = (WoStatus.COMPLETED, WoStatus.CLOSED)
    return {rules.OPEN: Q(work_order__status__in=OPEN_STATUSES),
            rules.NOT_CONFIRMED: Q(work_order__status__in=finished, outcome=""),
            rules.DONE: Q(work_order__isnull=True, removed_on__isnull=True) | Q(work_order__status__in=finished, outcome__in=RECORDED_OUTCOMES),
            rules.CANCELLED: Q(work_order__status=WoStatus.CANCELLED)}[state]


class SoftwareUpdateViewSet(_ReadOnly):
    """Slice 38: the software updates (the module docstring), read only, as the device's Security tab shows them (rules' words). Device
    security View and Equipment View (they name devices); never a scoped user."""

    permission_classes = [IsAuthenticated, ModulePermission, EquipmentView]
    serializer_class = scs.SoftwareUpdateSerializer
    view_refusal = UPDATES_REFUSAL
    write_refusal = UPDATES_WRITE_REFUSAL

    def http_method_not_allowed(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=UPDATES_READ_ONLY)

    def get_queryset(self):
        qs = (SoftwareUpdate.objects.filter(removed_on__isnull=True)
              .select_related("asset", "work_order", "target_release", "release_after", "recorded_by"))
        if self.action == "list":
            params, errors = self.request.query_params, {}
            device = params.get("device") or ""
            release = _id_filter(params, "release", "A software release's id.", errors)
            state = params.get("state") or ""
            if state and state not in rules.STATES:
                errors["state"] = [STATE_WORDS]
            if errors:
                raise DRFValidationError(errors)
            if device:
                pk = _uuid(device)
                qs = qs.filter(asset_id=pk) if pk is not None else qs.filter(asset__tag__iexact=device)
            if release is not None:
                qs = qs.filter(Q(target_release_id=release) | Q(release_after_id=release))
            if state:
                qs = qs.filter(state_q(state))
        # newest first, as the Security tab lists them (rules.updates_of): by the record's day, then the newest row
        day = Coalesce(F("applied_on"), F("work_order__completed_on"), F("opened_on"))
        return qs.order_by(day.desc(), "-created_at", "-pk")

    def get_object(self):
        pk = _uuid(self.kwargs.get(self.lookup_url_kwarg or self.lookup_field))
        found = self.get_queryset().filter(pk=pk).first() if pk is not None else None
        if found is None:
            raise NotFound(UPDATE_NOT_FOUND)
        self.check_object_permissions(self.request, found)
        return found


def register(router) -> None:
    router.register("software-releases", SoftwareReleaseViewSet, basename="software-release")
    router.register("device-security", DeviceSecurityViewSet, basename="device-security")
    router.register("software-updates", SoftwareUpdateViewSet, basename="software-update")  # slice 38
