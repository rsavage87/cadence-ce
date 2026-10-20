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
                                    ?department= a department's id; ?connected= connected | can_connect | not_connected | not_recorded
                                    (rules' connection keys); ?search= a tag or serial. Through rules.standings: a fixed number of queries.
GET device-security/<id or tag>/    One device on site, by id or by tag in any letter case.

Another facility's row is a 404 (as an unknown one), a retired device too; a level missing a 403 in plain words; any write a 405 naming
the Device security screen (with Device security Edit) or a 403 (without it).
"""
import uuid

from django.db.models import Count, F
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import MethodNotAllowed, NotFound
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.filters import SearchFilter
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Level, Module
from apps.cybersecurity import permissions as perms
from apps.cybersecurity import rules
from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease
from apps.equipment.models import Asset, AssetStatus

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
CONNECTED_FILTERS = (rules.CONNECTED, rules.CAN_CONNECT, rules.NOT_CONNECTED, rules.CONNECTION_NOT_RECORDED)


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

    def permission_denied(self, request, message=None, code=None):
        """ModulePermission's level refusal in plain words (a write: Edit, the default write_level, kept so that a write added later is
        closed until its level is set); the scoped refusal and EquipmentView's keep their own. With the level, a write is a 405."""
        if message is None and request.user and request.user.is_authenticated:
            message = self.view_refusal if request.method in SAFE_METHODS else WRITE_REFUSAL
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
    """The device list's second door: Equipment View (permissions.can_view_devices), checked after ModulePermission, so a scoped user
    keeps its own refusal."""

    def has_permission(self, request, view):
        if request.user.has_level(Module.EQUIPMENT, Level.VIEW):
            return True
        self.message = DEVICES_REFUSAL if request.method in SAFE_METHODS else WRITE_REFUSAL
        return False


class DeviceSecurityViewSet(_ReadOnly):
    """Each device on site with its facts and standing words (the module docstring). Device security View and Equipment View."""

    permission_classes = [IsAuthenticated, ModulePermission, EquipmentView]
    serializer_class = scs.DeviceSecuritySerializer
    view_refusal = DEVICES_REFUSAL
    lookup_value_regex = "[^/]+"  # a tag may hold a dot
    search_fields = ["tag", "serial"]

    def get_queryset(self):
        qs = rules.on_site()
        if self.action == "list":
            params, errors = self.request.query_params, {}
            model = _id_filter(params, "device_model", "A device model's id.", errors)
            department = _id_filter(params, "department", "A department's id.", errors)
            connected = params.get("connected") or ""
            if connected and connected not in CONNECTED_FILTERS:
                errors["connected"] = [f"One of {', '.join(CONNECTED_FILTERS)}."]
            if errors:
                raise DRFValidationError(errors)
            if model is not None:
                qs = qs.filter(device_model_id=model)
            if department is not None:
                qs = qs.filter(department_id=department)
        return qs.order_by("tag", "pk")

    def list(self, request, *args, **kwargs):
        """The devices with their standings (rules.standings: two queries for any number), then ?connected= on the words' keys, then
        the page."""
        found = list(rules.standings(self.filter_queryset(self.get_queryset()), self._today()).values())
        wanted = request.query_params.get("connected") or ""
        if wanted:
            found = [s for s in found if s.connection.key == wanted]
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


def register(router) -> None:
    router.register("software-releases", SoftwareReleaseViewSet, basename="software-release")
    router.register("device-security", DeviceSecurityViewSet, basename="device-security")
