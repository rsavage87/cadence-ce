"""
Import a CSV file from the command line (slice 23): the steps of Settings' Import data screen (apps.imports.services), for a file
larger than the screen takes, or an onboarding run by whoever sets the facility up.

    python manage.py import_data --tenant riverside --kind devices inventory.csv [--dry-run]

The file is read as the screen reads it (UTF-8, UTF-16 or Windows-1252; commas, semicolons, tabs, or pipes), with no limit on its
size or rows. Its columns are matched by name, the guess the screen offers before the person confirms it: a required column it
cannot find stops the command, naming the names it looks for and the file's columns (rename the column and run it again; each
kind's template on the screen has the names). Then the check: every row runs through the services and is put back, and the counts,
the notes grouped (skipped rows first, with their lines), how choice values were read, the totals, and what the import adds are
printed. Without --dry-run the import follows, a chunk at a time, and what it did is printed the same way.

The command line may import any kind (there is no user: the records' history names nobody). The run is kept as the screen keeps one
(Import data lists it), its rows cleared once it ends. A dry run, or a file that stops before its import begins, is discarded; an
import interrupted part way keeps the chunks it saved, and Continue on its page in Import data carries it on.

The facility is read from the Tenant table (a system table) before its tenant_context, so nothing tenant-scoped is touched before
(CLAUDE.md, non-negotiable 2).
"""
import os
import time

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.imports import kinds, services
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

Status = ImportRun.Status
# outcome, words for the check (what an import would do), words for the import (what it did)
OUTCOMES = (("create", "to add", "added"), ("update", "to update", "updated"), ("unchanged", "unchanged", "unchanged"), ("skip", "to skip", "skipped"))
LINES_SHOWN = 10   # line numbers printed per note
NAMES_SHOWN = 10   # names printed per group of what the import adds
ALIASES_SHOWN = 4  # names printed for a required column the file lacks


class Command(BaseCommand):
    help = "Check a CSV file of devices, contracts, technicians, or work orders, then import it (as Settings, Import data does)."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The CSV file")
        parser.add_argument("--tenant", required=True, help="The facility's slug")
        parser.add_argument("--kind", required=True, help=f"What the file holds: {', '.join(kinds.KINDS)}")
        parser.add_argument("--dry-run", action="store_true", help="Check the file and print what an import would do; save nothing")

    def handle(self, *args, **opts):
        importer = kinds.get(opts["kind"])
        if importer is None:
            raise CommandError(f"--kind must be one of {', '.join(kinds.KINDS)}; got {opts['kind']!r}.")
        tenant = Tenant.objects.filter(slug=opts["tenant"]).first()  # a system table: read before the facility's tenant_context
        if tenant is None:
            raise CommandError(f"No facility has the slug {opts['tenant']!r}.")
        try:
            with open(opts["path"], "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise CommandError(f"Cannot read {opts['path']}: {e.strerror or e}.") from None
        with tenant_context(tenant):
            try:
                run = services.upload(None, importer.kind, os.path.basename(opts["path"]), data, max_rows=None, max_bytes=None)
            except ValidationError as e:
                raise CommandError(" ".join(e.messages)) from None
            try:
                run = self._columns(run, importer)
                run = self._through(run)
                self._report(run, importer, imported=False)
                if opts["dry_run"]:
                    services.discard(run, None)
                    self.stdout.write(self.style.SUCCESS("Dry run: nothing was imported."))
                    return
                run = self._through(services.start_import(run, None))
            except ValidationError as e:
                self._end(run)
                raise CommandError(" ".join(e.messages)) from None
            except BaseException:
                self._end(run)
                raise
            self._report(run, importer, imported=True)

    def _columns(self, run, importer):
        """Confirm the columns the file names (services.mapping_of: the screen's guess by name), and start the check."""
        mapping = services.mapping_of(run)
        missing = [c for c in importer.required() if c.key not in mapping]
        if missing:
            wanted = "; ".join(f"{c.label} (a column named {', '.join(repr(a) for a in c.aliases[:ALIASES_SHOWN])})" for c in missing)
            raise ValidationError(f"The file has no column for {wanted}. Its columns are: {', '.join(repr(h) for h in run.header)}.")
        self.stdout.write("Columns: " + "; ".join(f"{importer.column(key).label} from {run.header[index]!r}" for key, index in mapping.items()))
        unused = [h for i, h in enumerate(run.header) if i not in set(mapping.values()) and h.strip()]
        if unused:
            self.stdout.write("Not read: " + ", ".join(repr(h) for h in unused))
        return services.confirm_columns(run, None, mapping)

    def _through(self, run):
        """The current pass (the check, or the import) to its end, a chunk at a time, as the screen's progress bar asks for them."""
        while run.status in (Status.CHECKING, Status.IMPORTING):
            before = run.offset
            run = services.process(run, None)
            if run.offset == before and run.status in (Status.CHECKING, Status.IMPORTING):
                time.sleep(0.5)  # someone on the screen holds the run and is doing this chunk
        return run

    def _end(self, run):
        """A file that stops before its import begins is discarded (its rows are the facility's data, kept no longer than needed). An
        import under way is left as it is: what it saved stays, and Continue on its page carries it on."""
        try:
            status = ImportRun.objects.filter(pk=run.pk).values_list("status", flat=True).first()
            if status in (Status.MAPPING, Status.CHECKING, Status.CHECKED):
                services.discard(run, None)
            elif status == Status.IMPORTING:
                self.stderr.write("The import stopped part way: the rows it saved stay. Continue it from its page in Settings, Import data.")
        except Exception:  # never hide the error that stopped the command
            pass

    def _report(self, run, importer, *, imported: bool):
        """The counts, the notes grouped (skipped rows first), and the importer's summary: how choice values were read, the totals to
        compare with the old system, and the names the import adds. Notes are words and keys, never a cell's free text."""
        words = {key: (done if imported else ahead) for key, ahead, done in OUTCOMES}
        counts = ", ".join(f"{run.counts.get(key, 0):,} {words[key]}" for key, _ahead, _done in OUTCOMES)
        what = f"{run.file_name} ({importer.label.lower()}, {run.row_count:,} row{'s' if run.row_count != 1 else ''})"
        line = f"{'Imported' if imported else 'Checked'} {what}: {counts}."
        self.stdout.write(self.style.SUCCESS(line) if imported else line)
        for g in services.problem_groups(run, lines_shown=LINES_SHOWN):
            more = g["count"] - len(g["lines"])
            where = ", ".join(str(n) for n in g["lines"]) + (f" and {more:,} more" if more else "")
            keys = f" ({', '.join(g['keys'])})" if g["keys"] else ""
            self.stdout.write(f"  {'Skipped' if g['skipped'] else 'Note'}, {g['count']:,} row{'s' if g['count'] != 1 else ''}: {g['note']}")
            self.stdout.write(f"    line{'s' if len(g['lines']) != 1 else ''} {where}{keys}")
        if len(run.results) >= services.RESULTS_KEPT:
            self.stdout.write(f"  (only the first {services.RESULTS_KEPT:,} rows with a note are kept)")
        summary = run.summary or {}
        for column, found in summary.get("values", {}).items():
            read = sorted(found.items(), key=lambda item: (-item[1][1], item[0]))
            read = [f"{repr(value) if value else '(blank)'} as {shown} ({count:,})" for value, (shown, count) in read]
            self.stdout.write(f"  {column} read: " + "; ".join(read))
        for group, labels in summary.get("totals", {}).items():
            self.stdout.write(f"  {group}: " + ", ".join(f"{label} {amount}" for label, amount in labels.items()))
        for group, names in summary.get("created", {}).items():
            listed = ", ".join(names[:NAMES_SHOWN]) + (", ..." if len(names) > NAMES_SHOWN else "")
            self.stdout.write(f"  {'Added' if imported else 'Adds'} {group.lower()} ({len(names):,}): {listed}")
