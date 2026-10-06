"""Technicians (slice 23, part C builds it): the people work orders and credentials name. Scaffold stub."""
from apps.accounts.models import Level, Module

from ..base import Column, Importer, RowSkip


class TechniciansImporter(Importer):
    kind = "technicians"
    label = "Technicians"
    module = Module.USERS
    level = Level.EDIT
    key = "name"
    order = 30
    description = "Your technicians, current and former, by name, so imported work orders name who did the work."
    columns = [Column("name", "Name", ("name", "technician", "technician name"), required=True, max_length=120, example="Dana Whitfield")]

    def apply(self, ctx, row, result):
        raise RowSkip("Not built yet")
