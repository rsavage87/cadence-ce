"""Devices (slice 23, part A builds it): the facility's equipment inventory. Scaffold stub."""
from apps.accounts.models import Level, Module

from ..base import Column, Importer, RowSkip


class DevicesImporter(Importer):
    kind = "devices"
    label = "Devices"
    module = Module.EQUIPMENT
    level = Level.EDIT
    key = "tag"
    order = 10
    description = "Your equipment inventory: one row per device, by its asset tag. Devices already here are updated."
    columns = [Column("tag", "Asset tag", ("asset tag", "tag", "control number", "control no"), required=True, max_length=40, example="CE-10042")]

    def apply(self, ctx, row, result):
        raise RowSkip("Not built yet")
