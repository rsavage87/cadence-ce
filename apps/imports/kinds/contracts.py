"""Service contracts and the devices they cover (slice 23, part C builds it). Scaffold stub."""
from apps.accounts.models import Level, Module

from ..base import Column, Importer, RowSkip


class ContractsImporter(Importer):
    kind = "contracts"
    label = "Service contracts"
    module = Module.CONTRACTS
    level = Level.EDIT
    key = "reference"
    order = 20
    description = "Service contracts, by reference, with the devices each covers."
    columns = [Column("reference", "Contract reference", ("contract reference", "reference", "contract number", "contract #"), required=True,
                      max_length=60, example="SC-2026-118")]

    def prepare(self, rows):
        return {}  # a reference repeats on purpose: one row per covered device

    def apply(self, ctx, row, result):
        raise RowSkip("Not built yet")
