"""
The steps of an import (slice 23), for the Import data screen (apps.web.views_imports) and `manage.py import_data`:

1. upload: the file is read (apps.imports.base.decode, read_table) and kept on a new ImportRun with every column (status mapping).
2. confirm_columns: the person confirms which file column feeds each of the importer's (auto_map's guess, which they may change);
   only those columns are kept from here on, the importer's prepare() finds what is wrong with the file as a whole (a key that
   repeats), and the check begins (checking).
3. process: one chunk of CHUNK rows of the current pass, in its own transaction, the screen asking for the next until done. The
   check runs every row through the real services in a savepoint and rolls the chunk back, so it reports exactly what the import
   will do; the import runs them again and commits each chunk with the run's progress, so an import that stops (a closed tab, a
   restart) carries on where it was, and the natural keys make a repeated row harmless. No request runs long or holds the
   facility's locks (its work-order numbering) for more than a chunk.
4. start_import: checked -> importing. One import at a time per facility (a constraint, so two clicks cannot both start one).
5. discard: ends a run that is not done (an import already under way keeps the chunks it committed). expire_stale ends runs left
   unfinished for EXPIRE_DAYS. Every ending clears the stored rows.

A row an importer cannot take (RowSkip, a service's ValidationError or PermissionDenied, a database refusal) is skipped with its
reason and never stops the file. No email goes out while a chunk runs (apps.notifications.assignments.quiet). Permissions are
checked on the run's kind at every step (apps.imports.permissions), never on the page alone.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, OperationalError, transaction
from django.utils import timezone

from apps.notifications import assignments

from . import base, kinds
from .models import ImportRun
from .permissions import can_import

logger = logging.getLogger(__name__)

CHUNK = 200
EXPIRE_DAYS = 14
CREATED_SHOWN = 500  # names kept per group of "what the import adds"
RESULTS_KEPT = 5_000  # rows with notes kept on a run (a file whose every row has a note says the same thing many times over)
Status = ImportRun.Status


def _importer(run_or_kind):
    kind = run_or_kind if isinstance(run_or_kind, str) else run_or_kind.kind
    importer = kinds.get(kind)
    if importer is None:
        raise ValidationError("Choose what the file holds.")
    return importer


def _check(user, run_or_kind) -> None:
    kind = run_or_kind if isinstance(run_or_kind, str) else run_or_kind.kind
    if not can_import(user, kind):
        raise PermissionDenied


def _locked(run) -> ImportRun:
    return ImportRun.objects.select_for_update().get(pk=run.pk)


def _restart_pass(run) -> None:
    run.offset, run.results, run.counts, run.summary = 0, [], {}, {}


# --- 1. upload ------------------------------------------------------------------------------------------------------------------

def upload(user, kind: str, file_name: str, data: bytes, *, max_rows: int | None = base.MAX_ROWS,
           max_bytes: int | None = base.MAX_BYTES) -> ImportRun:
    """A new run holding the file's header and rows. Raises ValidationError for a file it cannot read or that is too large."""
    _importer(kind)
    _check(user, kind)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValidationError(f"The file is larger than {max_bytes // (1024 * 1024)} MB. Split it (by year, or by department) and import the parts in turn.")
    header, rows = base.read_table(base.decode(data), max_rows=max_rows)
    expire_stale()
    return ImportRun.objects.create(kind=kind, file_name=(file_name or "upload.csv")[:200], uploaded_by=user, header=header, rows=rows,
                                    row_count=len(rows))


# --- 2. columns -----------------------------------------------------------------------------------------------------------------

def mapping_of(run) -> dict[str, int]:
    """Column key -> header index: the confirmed one, or auto_map's guess while the person has not confirmed."""
    if run.columns:  # confirmed: the run keeps only those columns, in this order
        return {key: i for i, key in enumerate(run.columns)}
    return base.auto_map(_importer(run).columns, run.header)


def mapping_view(run) -> dict:
    """What the columns step shows: each of the importer's columns with the file column it reads (index or None) and that column's
    sample values; the file's columns nothing reads; and column names the file repeats."""
    importer = _importer(run)
    guess = base.auto_map(importer.columns, run.header)
    used = set(guess.values())
    return {
        "columns": [{"column": c, "index": guess.get(c.key), "samples": base.samples(run.rows, guess[c.key]) if c.key in guess else []}
                    for c in importer.columns],
        "unused": [h for i, h in enumerate(run.header) if i not in used and h.strip()],
        "duplicates": base.duplicate_headers(run.header),
        "header": list(enumerate(run.header)),
    }


@transaction.atomic
def confirm_columns(run, user, mapping: dict[str, int | None]) -> ImportRun:
    """Keep the file columns `mapping` names (column key -> header index; None or absent: not in the file), drop the rest, find
    the file's problems, and start the check."""
    run = _locked(run)
    _check(user, run)
    if run.status != Status.MAPPING:
        raise ValidationError("The columns of this file are already chosen.")
    importer = _importer(run)
    chosen = {}
    for column in importer.columns:
        index = mapping.get(column.key)
        if index is None or index == "":
            continue
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValidationError(f"Choose a column of the file for {column.label}.") from None
        if not 0 <= index < len(run.header):
            raise ValidationError(f"Choose a column of the file for {column.label}.")
        chosen[column.key] = index
    missing = [c.label for c in importer.required() if c.key not in chosen]
    if missing:
        raise ValidationError(f"Choose the file's column for {', '.join(missing)}: {'it is' if len(missing) == 1 else 'they are'} needed.")
    twice = [run.header[i] for i in set(chosen.values()) if list(chosen.values()).count(i) > 1]
    if twice:
        raise ValidationError(f"Each column of the file can feed one value: {', '.join(twice)} is chosen more than once.")
    keys = [c.key for c in importer.columns if c.key in chosen]
    run.columns = keys
    run.header = [run.header[chosen[k]] for k in keys]
    run.rows = [[row[chosen[k]] for k in keys] for row in run.rows]
    dicts = [dict(zip(keys, (base.clean(c) for c in cells))) for cells in run.rows]
    run.file_problems = {str(i): reason for i, reason in importer.prepare(dicts).items()}
    run.status = Status.CHECKING
    _restart_pass(run)
    run.save()
    return run


# --- 3. the passes --------------------------------------------------------------------------------------------------------------

def process(run, user) -> ImportRun:
    """Run the next chunk of the current pass (the check, or the import) and return the run. A run that is not checking or
    importing is returned as it is; so is one another request is working on right now (the screen asks again)."""
    with transaction.atomic():
        try:
            with transaction.atomic():  # a savepoint: a refused lock must leave the transaction usable
                run = ImportRun.objects.select_for_update(nowait=True).get(pk=run.pk)
        except OperationalError:  # another request holds the run: it is doing this chunk
            return ImportRun.objects.get(pk=run.pk)
        _check(user, run)
        if run.status not in (Status.CHECKING, Status.IMPORTING):
            return run
        importing = run.status == Status.IMPORTING
        importer = _importer(run)
        start = run.offset
        end = min(start + getattr(importer, "chunk", CHUNK), run.row_count)
        rows = [dict(zip(run.columns, (base.clean(c) for c in cells))) for cells in run.rows[start:end]]
        ctx = base.Context(user=user, today=timezone.localdate(), check=not importing)
        results = []
        chunk = transaction.savepoint()
        with assignments.quiet():
            importer.load(ctx, rows)
            for i, row in enumerate(rows):
                result = base.RowResult(line=start + i + 2, key=(row.get(importer.key) or "")[:80])
                problem = run.file_problems.get(str(start + i))
                if problem:
                    result.outcome, result.notes = result.SKIP, [problem]
                else:
                    _apply(importer, ctx, row, result)
                results.append(result)
        if importing:
            transaction.savepoint_commit(chunk)
        else:
            transaction.savepoint_rollback(chunk)  # the check changes nothing
        _record(run, results, ctx)
        run.offset = end
        if end >= run.row_count:
            _finish(run, user, importing)
        run.save()
    return run


def _apply(importer, ctx, row: dict, result) -> None:
    long = next((c for c in importer.columns if c.max_length and len(row.get(c.key) or "") > c.max_length), None)
    if long:
        result.outcome, result.notes = result.SKIP, [f"{long.label} is longer than {long.max_length} characters"]
        return
    point = transaction.savepoint()
    try:
        importer.apply(ctx, row, result)
    except base.RowSkip as e:
        _skipped(point, result, str(e))
    except ValidationError as e:
        _skipped(point, result, "; ".join(e.messages))
    except PermissionDenied as e:
        _skipped(point, result, str(e) or "You may not make this change")
    except DatabaseError:
        logger.warning("import %s line %s: the database refused the row", importer.kind, result.line, exc_info=True)  # never the values
        _skipped(point, result, "It could not be saved (a value the database refuses)")
    else:
        transaction.savepoint_commit(point)


def _skipped(point, result, reason: str) -> None:
    transaction.savepoint_rollback(point)
    result.outcome, result.notes = result.SKIP, [reason]


def _record(run, results, ctx) -> None:
    for r in results:
        run.counts[r.outcome] = run.counts.get(r.outcome, 0) + 1
        if r.notes and len(run.results) < RESULTS_KEPT:
            run.results.append(r.as_list())
    summary = run.summary
    for group, labels in ctx.totals.items():
        bucket = summary.setdefault("totals", {}).setdefault(group, {})
        for label, amount in labels.items():
            bucket[label] = str(Decimal(bucket.get(label, "0")) + Decimal(amount))
    for group, names in ctx.created.items():
        kept = summary.setdefault("created", {}).setdefault(group, [])
        for name in names:
            if name not in kept and len(kept) < CREATED_SHOWN:
                kept.append(name)
    for column, values in ctx.values.items():
        bucket = summary.setdefault("values", {}).setdefault(column, {})
        for value, (shown, count) in values.items():
            entry = bucket.setdefault(value, [shown, 0])
            entry[1] += count


def _finish(run, user, importing: bool) -> None:
    now = timezone.now()
    if importing:
        run.status, run.imported_at, run.rows = Status.IMPORTED, now, []  # the facility's data now lives in its records
    else:
        run.status, run.checked_at = Status.CHECKED, now


# --- 4. import, 5. discard -------------------------------------------------------------------------------------------------------

@transaction.atomic
def start_import(run, user) -> ImportRun:
    run = _locked(run)
    _check(user, run)
    if run.status != Status.CHECKED:
        raise ValidationError("Only a checked file can be imported." if run.status != Status.IMPORTING else "This file is already importing.")
    run.status, run.imported_by = Status.IMPORTING, user
    _restart_pass(run)
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError:
        raise ValidationError("Another import is running in this facility. Wait for it to finish, then import this file.") from None
    return run


@transaction.atomic
def discard(run, user) -> ImportRun:
    """End a run that is not done and clear its rows. An import under way stops after the chunks it has committed."""
    run = _locked(run)
    _check(user, run)
    if run.done:
        raise ValidationError("This run has already ended.")
    run.status, run.rows = Status.DISCARDED, []
    run.save()
    return run


def expire_stale(now=None) -> int:
    """End the facility's runs left unfinished for EXPIRE_DAYS (their rows are the facility's data, kept no longer than needed)."""
    cutoff = (now or timezone.now()) - timedelta(days=EXPIRE_DAYS)
    stale = ImportRun.objects.exclude(status__in=[Status.IMPORTED, Status.DISCARDED, Status.EXPIRED]).filter(updated_at__lt=cutoff)
    return stale.update(status=Status.EXPIRED, rows=[], updated_at=timezone.now())


# --- reading a run ------------------------------------------------------------------------------------------------------------------

def runs_for(user):
    """The facility's runs of the kinds `user` may import, newest first."""
    return ImportRun.objects.filter(kind__in=[imp.kind for imp in kinds.KINDS.values() if can_import(user, imp.kind)])


def problem_groups(run, *, lines_shown: int = 20) -> list[dict]:
    """The run's notes, grouped: each distinct note once, whether rows with it were skipped or imported with it, how many, and the
    first lines and keys. Most skipped first."""
    groups = {}
    for line, key, outcome, notes in run.results:
        for note in notes:
            g = groups.setdefault((outcome == "skip", note), {"skipped": outcome == "skip", "note": note, "count": 0, "lines": [], "keys": []})
            g["count"] += 1
            if len(g["lines"]) < lines_shown:
                g["lines"].append(line)
                if key:
                    g["keys"].append(key)
    return sorted(groups.values(), key=lambda g: (not g["skipped"], -g["count"], g["note"]))


def progress(run) -> int:
    """How far the current pass is, in percent."""
    return 100 if not run.row_count else min(100, run.offset * 100 // run.row_count)
