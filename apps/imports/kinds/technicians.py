"""
Technicians (slice 23): the people work orders and credentials name, current and former. A former technician imports as inactive,
so the work order history imported after this file can name who did the work without offering them for new work.

A row names its technician by name, in any letter case and in either order ("Dana Whitfield" or "Whitfield, Dana", in the file or
in Cadence; a comma before a suffix or a credential, "John Smith, Jr.", is not Last, First): one technician with that name is
updated (a blank cell or a column not in the file keeps what Cadence has), two are a skip (which one is meant is for the person to
say), none is a new technician, written First Last. Names are read and compared by apps.credentials.services' technician_name and
name_key, as the work order import and Invite user's technician profile compare them. A technician linked to a user account keeps
the link: the import never touches it. Through apps.credentials.services (create_technician, update_technician).
"""
from apps.accounts.models import Level, Module
from apps.credentials import services as credentials
from apps.credentials.models import Technician

from .. import parse
from ..base import Column, Importer, RowSkip

ACTIVE_WORDS = {**{w: "active" for w in (*parse.YES, "active", "current", "employed", "a")},
                **{w: "inactive" for w in (*parse.NO, "inactive", "former", "terminated", "separated", "left", "retired", "i")}}
SHOWN = {"active": "Active", "inactive": "Inactive"}
LABELS = {"title": "Title", "certification": "Certification", "weekly_capacity_hours": "Weekly hours", "is_active": "Active"}


class TechniciansImporter(Importer):
    kind = "technicians"
    label = "Technicians"
    module = Module.USERS
    level = Level.EDIT
    key = "name"
    order = 30
    description = ("Your technicians, current and former, by name, so imported work orders name who did the work. Former staff import as "
                   "inactive. Technicians already here are updated; a blank cell keeps what is here.")
    columns = [
        Column("name", "Name", ("name", "technician", "technician name", "tech name", "full name", "employee name"), required=True,
               max_length=120, help="First Last or Last, First", example="Dana Whitfield"),
        Column("title", "Title", ("title", "job title", "position"), max_length=80, example="BMET II"),
        Column("certification", "Certification", ("certification", "certifications", "cert", "certs"), max_length=80, example="CBET"),
        Column("weekly_hours", "Weekly hours", ("weekly hours", "hours per week", "weekly capacity", "capacity hours", "scheduled hours"),
               help="Hours a week for planned work, 0 to 80", example="32"),
        Column("active", "Active", ("active", "is active", "status", "employment status"), help="Yes, or no for someone who has left",
               example="Yes"),
    ]

    def prepare(self, rows):
        """A technician named twice, in either order ("Dana Whitfield" and "Whitfield, Dana"), is a problem of the file."""
        firsts, problems = {}, {}
        for i, row in enumerate(rows):
            key = credentials.name_key(row.get("name"))
            if not key:
                continue
            if key in firsts:
                problems[i] = f"Name {row['name']} is also on line {firsts[key] + 2}: one row per technician"
            else:
                firsts[key] = i
        return problems

    def load(self, ctx, rows):
        names = ctx.cache.setdefault("names", {})
        for pk, name in Technician.objects.values_list("pk", "name"):
            names.setdefault(credentials.name_key(name), []).append(pk)

    def apply(self, ctx, row, result):
        name = credentials.technician_name(row["name"])
        if not name:
            raise RowSkip("Name is blank")
        matches = ctx.cache["names"].get(credentials.name_key(name), [])
        if len(matches) > 1:
            raise RowSkip("Two technicians have this name" if len(matches) == 2 else f"{len(matches)} technicians have this name")
        technician = Technician.objects.filter(pk=matches[0]).first() if matches else None
        values, reads = self._read(row, result, new=technician is None)
        if technician is None:
            technician = credentials.create_technician(name=name, **values)
            result.outcome = result.CREATE
            ctx.add_created("Technicians", technician.name)
            ctx.total("New technicians", "active" if technician.is_active else "inactive")
        else:
            changes = {field: value for field, value in values.items() if getattr(technician, field) != value}
            if changes:
                credentials.update_technician(technician, **changes)
                result.outcome = result.UPDATE
                for field in changes:
                    ctx.total("Changes", LABELS[field])
        for value, slug in reads:
            ctx.read_as("Active", value, SHOWN[slug])

    def _read(self, row, result, *, new: bool) -> tuple[dict, list]:
        """The row's values for the technician (a blank cell, or a column not in the file, is left out) and how the Active values
        were read. A value it cannot read is left out too, with a note: a new technician gets the default, one here keeps its own."""
        values, reads = {}, []
        for field in ("title", "certification"):
            if row.get(field):
                values[field] = row[field]
        try:
            hours = parse.parse_decimal(row.get("weekly_hours", ""), places=1, low=0, high=credentials.MAX_WEEKLY_HOURS, what="number of hours")
        except parse.Unreadable:
            kept = f"{credentials.DEFAULT_WEEKLY_HOURS} used" if new else "left as it was"
            result.warn(f"Weekly hours not read (0 to {credentials.MAX_WEEKLY_HOURS}): {kept}")
        else:
            if hours is not None:
                values["weekly_capacity_hours"] = hours
        try:
            active = parse.parse_choice(row.get("active", ""), ACTIVE_WORDS, what="yes or no")
        except parse.Unreadable:
            result.warn("Active not read: imported as active" if new else "Active not read: left as it was")
        else:
            if active is not None:
                values["is_active"] = active == "active"
                reads.append((row["active"], active))
        return values, reads
