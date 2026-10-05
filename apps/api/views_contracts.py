"""
Contracts (slice 19, part A): the contracts and the devices they cover.
"""


from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.contracts.models import Contract
from apps.equipment.models import Asset

from . import serializers as s
from .base import TenantViewSet


class ContractViewSet(TenantViewSet):
    model, module, serializer_class = Contract, "contracts", s.ContractSerializer
    search_fields = ["reference", "vendor"]

    @action(detail=True, methods=["post"])
    def add_assets(self, request, pk=None):
        """Body: {"asset_ids": [...]} or {"device_model": "<id>"} to add every active device of a model."""
        contract = self.get_object()
        if request.data.get("device_model"):
            assets = Asset.objects.filter(device_model_id=request.data["device_model"], status__in=Asset.ACTIVE_STATUSES)
        else:
            assets = Asset.objects.filter(id__in=request.data.get("asset_ids", []))
        n = contract.add_assets(assets)
        return Response({"added": n, "device_count": contract.covered_assets().count()})

    @action(detail=True, methods=["post"])
    def remove_asset(self, request, pk=None):
        contract = self.get_object()
        asset = Asset.objects.filter(pk=request.data.get("asset_id")).first()
        if asset is None:
            return Response({"detail": "asset_id required"}, status=status.HTTP_400_BAD_REQUEST)
        contract.remove_asset(asset)
        return Response({"device_count": contract.covered_assets().count()})


def register(router):
    router.register("contracts", ContractViewSet, basename="contract")
