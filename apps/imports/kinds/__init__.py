"""
The kinds of file a facility can import (slice 23), in onboarding order: devices first (everything else names them by tag), then
contracts (with the devices they cover), technicians, and work order history. Each is an apps.imports.base.Importer.
"""
from .contracts import ContractsImporter
from .devices import DevicesImporter
from .technicians import TechniciansImporter
from .work_orders import WorkOrdersImporter

KINDS = {imp.kind: imp for imp in sorted((DevicesImporter(), ContractsImporter(), TechniciansImporter(), WorkOrdersImporter()),
                                         key=lambda i: i.order)}


def get(kind: str):
    """The importer for `kind`, or None."""
    return KINDS.get(kind)
