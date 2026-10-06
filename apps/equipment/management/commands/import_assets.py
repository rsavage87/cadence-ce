"""
Import an equipment inventory from a CSV export of another CMMS (MediMizer, AIMS, TMS, EQ2, Nuvolo, or a spreadsheet): the Import
data screen's Devices import (apps.imports.kinds.devices), from the command line and without the screen's size limits.

    python manage.py import_assets --tenant riverside inventory.csv [--dry-run]

Columns are matched by name, in any letter case (DevicesImporter.columns lists the names each one answers to); the asset tag, the
manufacturer, and the model must be there. Every row is checked first through the equipment services, which changes nothing; with
--dry-run that is all. Otherwise the file is then imported chunk by chunk: a row it cannot take is skipped with its reason and never
stops the file, and a device already here (its tag, in any letter case) is updated where the file has a value, never doubled.
Unknown models and departments are added; a model already in the catalog keeps its details (risk class, interval, the CMS mark).

It prints the counts, the notes grouped (skipped rows on stderr), and the totals to check against the old system. The file's text
may be UTF-8 (with or without a byte-order mark), UTF-16, or Windows-1252, separated by commas, semicolons, tabs, or pipes.
"""
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.imports import kinds, services
from apps.imports.kinds.devices import DevicesImporter
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

LINES_SHOWN = 10  # lines listed under each note


def _amount(value: str) -> str:
    number = Decimal(value)
    return f"{int(number):,}" if number == number.to_integral_value() else f"{number:,.2f}"


class Command(BaseCommand):
    help = "Import devices from a CSV export of another CMMS: checked first, then imported (or only checked, with --dry-run)."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--dry-run", action="store_true", help="Check the file and report; change nothing.")

    def handle(self, *args, **opts):
        tenant = Tenant.objects.filter(slug=opts["tenant"]).first()
        if tenant is None:
            raise CommandError(f"No tenant with slug {opts['tenant']}")
        path = Path(opts["path"])
        try:
            data = path.read_bytes()
        except OSError as e:
            raise CommandError(f"Could not read {path}: {e.strerror}") from None
        importer = kinds.get(DevicesImporter.kind)
        with tenant_context(tenant):  # the facility's rows, its time zone (its today), and the Postgres app.tenant_id
            try:
                run = services.upload(None, importer.kind, path.name, data, max_rows=None, max_bytes=None)
            except ValidationError as e:
                raise CommandError(" ".join(e.messages)) from None
            mapping = services.mapping_of(run)
            missing = [c.label for c in importer.required() if c.key not in mapping]
            if missing:
                services.discard(run, None)
                raise CommandError(f"Could not find a column for {', '.join(missing)}. Columns seen: {', '.join(run.header)}")
            self.stdout.write("Column mapping: " + ", ".join(f"{importer.column(k).label} <- {run.header[i]}" for k, i in mapping.items()))
            run = self._through(services.confirm_columns(run, None, mapping))
            if opts["dry_run"]:
                self._report(run, "Dry run (nothing saved)")
                services.discard(run, None)  # its rows are the facility's data: kept no longer than needed
                return
            try:
                run = services.start_import(run, None)
            except ValidationError as e:
                services.discard(run, None)
                raise CommandError(" ".join(e.messages)) from None
            self._report(self._through(run), "Imported")

    def _through(self, run):
        """Run the current pass (the check, or the import) chunk by chunk to its end."""
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = services.process(run, None)
        return run

    def _report(self, run, heading: str) -> None:
        counts = run.counts
        self.stdout.write(self.style.SUCCESS(f"{heading}: {counts.get('create', 0)} created, {counts.get('update', 0)} updated, "
                                             f"{counts.get('skip', 0)} skipped, {counts.get('unchanged', 0)} unchanged"))
        for group in services.problem_groups(run, lines_shown=LINES_SHOWN):
            rows = f"{group['count']} row{'' if group['count'] == 1 else 's'}"
            more = group["count"] - len(group["lines"])
            where = f"line{'' if len(group['lines']) == 1 else 's'} {', '.join(map(str, group['lines']))}{f' and {more} more' if more else ''}"
            if group["keys"]:
                where += f" ({', '.join(group['keys'])})"
            if group["skipped"]:
                self.stderr.write(f"Skipped {rows}: {group['note']}\n    {where}")
            else:
                self.stdout.write(f"{rows} with a note: {group['note']}\n    {where}")
        summary = run.summary
        for group, labels in summary.get("totals", {}).items():
            self.stdout.write(f"{group}: " + ", ".join(f"{label} {_amount(amount)}" for label, amount in labels.items()))
        for group, names in summary.get("created", {}).items():
            self.stdout.write(f"{group} added: {', '.join(names)}")
        for column, values in summary.get("values", {}).items():
            self.stdout.write(f"{column} read as: " + ", ".join(f"{value!r} -> {shown} ({count})" for value, (shown, count) in values.items()))
