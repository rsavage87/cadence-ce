"""Work order history and open backlog (slice 23, part B builds it). Scaffold stub."""
from apps.accounts.models import Level, Module

from ..base import Column, Importer, RowSkip


class WorkOrdersImporter(Importer):
    kind = "work_orders"
    label = "Work orders"
    module = Module.WORKORDERS
    level = Level.APPROVE
    key = "legacy_number"
    order = 40
    description = "Work order history and open work from your previous system, one row per work order, with its hours and costs."
    columns = [Column("legacy_number", "Work order number", ("work order number", "wo number", "wo #", "work order #", "wo no"), required=True,
                      max_length=40, example="10423")]

    def apply(self, ctx, row, result):
        raise RowSkip("Not built yet")
