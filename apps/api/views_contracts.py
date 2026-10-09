"""
Contracts (slice 19, part A): the contracts and the devices they cover, through apps.contracts.services as the Contracts screen
changes them (apps/web/views_contracts.py), with its levels, checked server-side on every request:

GET    /api/v1/contracts/                         the contracts, soonest to end first (?search= reference or vendor). Contracts View.
GET    /api/v1/contracts/<id>/                    one contract. Contracts View.
POST   /api/v1/contracts/                         create_contract. Body: {"reference", "vendor", "type" (oem | third_party), "coverage"
                                                  (full | parts_labor | parts | pm_only | tm), "start_on", "end_on" (YYYY-MM-DD),
                                                  "annual_cost", "notes" (at most 500 characters)}; reference, vendor, and the dates
                                                  are required. Contracts Edit. 201 with the contract.
PUT    /api/v1/contracts/<id>/                    update_contract, every field; PATCH any of them. Contracts Edit. A new type moves
PATCH                                             the covered devices' support type with it. 200 with the contract.
POST   /api/v1/contracts/<id>/renew/              renew_contract: twelve more months from the current end, or from today when it
                                                  has lapsed. No body. Contracts Edit. 200 with the contract.
DELETE /api/v1/contracts/<id>/                    delete_contract: its devices go back to in-house support first. Contracts Full.
                                                  200: {"id", "reference", "uncovered": how many devices in use lost coverage}.
POST   /api/v1/contracts/<id>/add_assets/         put devices on the contract. Body: {"asset_ids": ["<id>", ...]} (add_asset for each,
                                                  all or none: a retired device refuses the batch) or {"device_model": "<id>"}
                                                  (add_model: every device of ours of the model in use that is not on it yet; never a
                                                  rental, vendor loaner, or demo unit, which add_asset refuses too). A device on
                                                  another contract moves here (a device is on one contract at most). Contracts Edit.
                                                  200: {"added": how many came onto the contract, "devices": [{"asset_id", "tag",
                                                  "from_contract": the reference it left, or null when it was in-house}],
                                                  "device_count": the devices it covers now}.
POST   /api/v1/contracts/<id>/remove_asset/       remove_asset. Body: {"asset_id": "<id>"}; a device not on this contract is a 400.
                                                  Contracts Edit. 200: {"removed": {"asset_id", "tag"}, "device_count"}.

A service's refusal is a 400 keyed by field, a field an endpoint does not take a 400 ("Unknown field."; a write may send back
id, status, device_count, and updated_at as GET returned them), another facility's contract a 404, and another facility's device or
model in a body a 400, as an unknown id. Closed to scoped users (apps.workorders.scoping), as the Contracts screen is.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.contracts import services as ct
from apps.contracts.models import Contract
from apps.equipment.models import Asset, AssetStatus, DeviceModel
from apps.tenants.context import get_current_tenant

from . import serializers_contracts as sc
from .base import TenantViewSet, _via_service


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _as(field, fn, *args, **kwargs):
    """A service call whose refusal is about one field of the body: its message keyed by that field."""
    try:
        return fn(*args, **kwargs)
    except ValidationError as e:
        raise DRFValidationError({field: e.messages}) from e


def _device(asset, from_contract=None) -> dict:
    return {"asset_id": str(asset.id), "tag": asset.tag, "from_contract": from_contract.reference if from_contract else None}


class ContractViewSet(TenantViewSet):
    # Writes need Contracts Edit (TenantViewSet's write_level) and deleting Full (its delete_level), as the Contracts screen.
    model, module, serializer_class = Contract, "contracts", sc.ContractSerializer
    search_fields = ["reference", "vendor"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:  # a superuser who has not picked a tenant would otherwise read or write nobody's contracts
            raise PermissionDenied("Pick a tenant first (Admin, Tenants).")

    def get_queryset(self):
        # As the Contracts table counts them (contracts.services.filter_contracts), in its order: an aggregate drops Meta.ordering.
        return Contract.objects.annotate(devices=Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED))).order_by("end_on", "reference")

    def _parsed(self, instance=None, partial=False) -> dict:
        serializer = self.get_serializer(instance, data=self.request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        return dict(serializer.validated_data)

    def _shown(self, contract):
        return self.get_serializer(contract).data

    def _body(self, *allowed) -> dict:
        """A JSON object holding only `allowed` (a form post's token aside); anything else is refused, never ignored."""
        data = self.request.data
        if not hasattr(data, "keys"):
            raise DRFValidationError({"detail": "Send a JSON object."})
        unknown = sorted(set(data.keys()) - set(allowed) - {"csrfmiddlewaretoken"})
        if unknown:
            raise DRFValidationError({name: ["Unknown field."] for name in unknown})
        return data

    # --- the contract ----------------------------------------------------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        contract = _via_service(ct.create_contract, by=request.user, **self._parsed())
        return Response(self._shown(contract), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        contract = self.get_object()
        _via_service(ct.update_contract, contract, by=request.user, **self._parsed(contract, partial=kwargs.pop("partial", False)))
        return Response(self._shown(contract))

    def destroy(self, request, *args, **kwargs):
        contract = self.get_object()
        pk, reference = str(contract.pk), contract.reference
        uncovered = ct.delete_contract(contract)
        return Response({"id": pk, "reference": reference, "uncovered": uncovered})

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        contract = self.get_object()
        self._body()  # takes nothing: a term or a date sent here would otherwise look applied
        _via_service(ct.renew_contract, contract, by=request.user)
        return Response(self._shown(contract))

    # --- the devices it covers -------------------------------------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def add_assets(self, request, pk=None):
        contract = self.get_object()
        body = self._body("asset_ids", "device_model")
        ids = body.getlist("asset_ids") if hasattr(body, "getlist") else body.get("asset_ids")  # a form post repeats the key
        has_ids, has_model = ids not in (None, "", []), body.get("device_model") not in (None, "")
        if has_ids and has_model:
            raise DRFValidationError({"detail": "Send asset_ids or device_model, not both."})
        if has_model:
            return self._add_model(contract, body["device_model"])
        if not has_ids:
            raise DRFValidationError({"asset_ids": ["Send asset_ids (a list of device ids) or device_model (a model's id)."]})
        return self._add_assets(contract, ids)

    def _add_assets(self, contract, ids):
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise DRFValidationError({"asset_ids": ["Send a list of device ids."]})
        wanted = list(dict.fromkeys(ids))  # in the order sent, each once
        parsed = {i: _uuid(i) for i in wanted}
        found = {str(a.pk): a for a in Asset.objects.filter(pk__in=[u for u in parsed.values() if u]).select_related("contract")}
        missing = [i for i in wanted if parsed[i] is None or str(parsed[i]) not in found]
        if missing:
            raise DRFValidationError({"asset_ids": [f"Not a device in this facility: {', '.join(missing)}."]})
        devices = []
        with transaction.atomic():  # all or none, as on the screen's bulk moves: one retired device refuses the batch
            for i in wanted:
                asset = found[str(parsed[i])]
                was_here = asset.contract_id == contract.id
                previous = _as("asset_ids", ct.add_asset, contract, asset)
                if not was_here:
                    devices.append(_device(asset, previous))
        return Response({"added": len(devices), "devices": devices, "device_count": contract.covered_assets().count()})

    def _add_model(self, contract, value):
        pk = _uuid(value)
        dm = DeviceModel.objects.filter(pk=pk).first() if pk else None
        if dm is None:
            raise DRFValidationError({"device_model": ["Choose a device model from this facility."]})
        with transaction.atomic():
            # The devices add_model takes (ours in use, not on this contract yet: contracts.services.model_devices, so a rental, vendor
            # loaner, or demo unit is never reported as moved; slice 29), read first to say where each came from.
            moving = list(ct.model_devices(contract, dm).select_related("contract").order_by("tag"))
            added = ct.add_model(contract, dm)
        return Response({"added": added, "devices": [_device(a, a.contract) for a in moving], "device_count": contract.covered_assets().count()})

    @action(detail=True, methods=["post"])
    def remove_asset(self, request, pk=None):
        contract = self.get_object()
        body = self._body("asset_id")
        asset_pk = _uuid(body.get("asset_id"))
        asset = Asset.objects.filter(pk=asset_pk).first() if asset_pk else None
        if asset is None:
            raise DRFValidationError({"asset_id": ["Choose a device from this facility."]})
        _as("asset_id", ct.remove_asset, contract, asset)
        return Response({"removed": {"asset_id": str(asset.id), "tag": asset.tag}, "device_count": contract.covered_assets().count()})


def register(router):
    router.register("contracts", ContractViewSet, basename="contract")
