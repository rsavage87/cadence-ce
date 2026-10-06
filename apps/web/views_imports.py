"""
Import data (slice 23, part D): the Settings screen that takes a facility's files from the CMMS it is leaving, over
apps.imports.services. Views parse the form, call a step, and render; each step checks the user's level on the run's kind itself
(apps.imports.permissions), so a refused POST is the service's PermissionDenied (403) and a refused change its ValidationError,
shown on the page.

/settings/import/ (imports): the kinds the user may import (permissions.importable), in onboarding order, each with its description,
its columns, a template CSV (import_template: the header and an example row, which map themselves), and an upload form (the kind
and the file, up to base.MAX_BYTES and base.MAX_ROWS, posted to the page itself, so a refused file comes back beside its form);
below, the facility's runs of those kinds (services.runs_for), newest first, each linking to its page. Reading the page or a run
ends the runs left unfinished for services.EXPIRE_DAYS first (services.expire_stale), so none shows as waiting when it is not.

/settings/import/<id>/ (import_run), by the run's status:
- mapping: each of the importer's columns with a select of the file's columns (services.mapping_view: the guess chosen, the required
  ones marked) and the chosen column's sample values, which import_samples reads again when the choice changes; the file's columns
  nothing reads and the names it repeats. "Check the file" posts the choice (import_columns: services.confirm_columns).
- checking, importing: a progress bar that asks for the next chunk itself (web/_imports_progress.html posts to import_next on load;
  each answer is the bar again until the pass is done, then the browser loads the run's page). An import nobody has moved for
  STALLED (a closed tab, a restart) waits for Continue instead, and either pass can be stopped (services.discard).
- checked: what the import will do (the counts, the notes grouped with skipped rows first, how choice values were read, the totals,
  and what it adds), Import (import_start: services.start_import, then the progress) and Discard (import_discard), and every row
  with a note as a CSV (import_notes).
- imported, discarded, expired: what happened, read only.

Settings View opens the screen (scoped users never: web_view). A run is shown only to someone who may import its kind (runs_for: a
404 otherwise, another facility's too); the steps look the run up in the facility and leave the refusal to the service. The run's
stored rows (up to MAX_BYTES of the facility's data) are read only where they are needed: the column choice and its samples.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django_htmx.http import HttpResponseClientRedirect

from apps.accounts.models import Level, Module
from apps.imports import base, kinds, services
from apps.imports import permissions as imp_perms
from apps.imports.models import ImportRun

from .decorators import web_view
from .exports import csv_response

Status = ImportRun.Status
PAGE = (imp_perms.PAGE_MODULE, imp_perms.PAGE_LEVEL)
RUNS_SHOWN = 50
STALLED = timedelta(minutes=1)  # an import whose run has not moved for this long is not being polled: it waits for Continue
WAIT = "load delay:2s"          # the bar asks again after this when another request was doing the chunk
NAMES_SHOWN = 12                # names of "what the import adds" shown before "and N more"
# What the run list and the passes never read: the file's rows above all (up to MAX_BYTES of JSON)
HEAVY = ("rows", "header", "columns", "file_problems", "results", "summary")
# outcome, words for a check (what an import would do), words for an import (what it did)
OUTCOMES = (("create", "To add", "Added"), ("update", "To update", "Updated"), ("unchanged", "Unchanged", "Unchanged"),
            ("skip", "To skip", "Skipped"))
STATUS_CSS = {Status.MAPPING: "neutral", Status.CHECKING: "info", Status.CHECKED: "acc", Status.IMPORTING: "info", Status.IMPORTED: "ok",
              Status.DISCARDED: "neutral", Status.EXPIRED: "neutral"}


# --- reading runs ---------------------------------------------------------------------------------

def _run(request, pk) -> ImportRun:
    """A run of a kind the user may import (another kind's, or another facility's, is a 404), without its rows until they are read."""
    return get_object_or_404(services.runs_for(request.user).defer("rows").select_related("uploaded_by", "imported_by"), pk=pk)


def _step_run(pk) -> ImportRun:
    """The facility's run for a step, whatever its kind: the step itself refuses a kind the user may not import (403). Each step
    locks and reads the run again, so nothing of it is loaded here but its id."""
    return get_object_or_404(ImportRun.objects.defer(*HEAVY), pk=pk)


def _run_url(run) -> str:
    return reverse("web:import_run", args=[run.pk])


def _go(request, url):
    """Where a step's form sends the browser: an HTMX request navigates there, a plain form post is redirected."""
    return HttpResponseClientRedirect(url) if request.htmx else redirect(url)


def _import_started(run) -> bool:
    """Whether the run's counts are an import's (rows saved) rather than a check's (what an import would do)."""
    return run.status in (Status.IMPORTING, Status.IMPORTED) or run.imported_at is not None or run.imported_by_id is not None


def _counts(run) -> list[dict]:
    started = _import_started(run)
    return [{"key": key, "label": done if started else ahead, "n": run.counts.get(key, 0), "text": f"{run.counts.get(key, 0):,}"}
            for key, ahead, done in OUTCOMES]


def _counts_text(run) -> str:
    """The run list's line: rows by outcome, or how far a pass is, or the file's rows (waiting for their columns, or ended before
    its check finished: a part of a check's counts would read as the file's)."""
    if run.status == Status.MAPPING or (run.done and not (run.counts and (_import_started(run) or run.checked_at is not None))):
        return f"{run.row_count:,} row{'s' if run.row_count != 1 else ''}"
    if run.status in (Status.CHECKING, Status.IMPORTING):
        return f"{run.offset:,} of {run.row_count:,} rows"
    return ", ".join(f"{c['n']:,} {c['label'].lower()}" for c in _counts(run) if c["n"]) or "No rows"


def _row(run) -> dict:
    importer = kinds.get(run.kind)
    return {"run": run, "label": importer.label if importer else run.kind, "css": STATUS_CSS.get(run.status, "neutral"),
            "counts": _counts_text(run), "who": run.imported_by or run.uploaded_by}


def _amount(value: str) -> str:
    """A total as the screen shows it: with thousands separators, and the places the importer gave it (a count has none, an
    amount of money two: "1250.50" + "1250.50" is "2,501.00", never "2,501")."""
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError):
        return str(value)
    if not number.is_finite():
        return str(value)
    places = max(0, -number.as_tuple().exponent)
    return f"{number:,.{places}f}"


def _index(value, width: int) -> int | None:
    """A file column's index from a select's value, or None (not in the file, or not one of its columns)."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if 0 <= index < width else None


# --- the page -------------------------------------------------------------------------------------

def _page(request, *, error: str = "", error_kind: str = ""):
    user = request.user
    services.expire_stale()
    runs = services.runs_for(user).defer(*HEAVY).select_related("uploaded_by", "imported_by")[:RUNS_SHOWN]
    ctx = {"nav_active": "settings", "kinds": [{"importer": imp, "error": error if imp.kind == error_kind else ""} for imp in imp_perms.importable(user)],
           "error": error if error_kind not in kinds.KINDS or not imp_perms.can_import(user, error_kind) else "",
           "runs": [_row(r) for r in runs], "runs_shown": RUNS_SHOWN, "max_mb": base.MAX_BYTES // (1024 * 1024), "max_rows": f"{base.MAX_ROWS:,}",
           "levels": [{"label": imp.label, "module": Module(imp.module).label, "level": Level(imp.level).label} for imp in kinds.KINDS.values()]}
    return render(request, "web/imports.html", ctx)


@require_http_methods(["GET", "HEAD", "POST"])
@web_view(*PAGE)
def imports(request):
    """The page, and (POST) an upload, which posts to the page's own address so a refused file comes back to it."""
    return _upload(request) if request.method == "POST" else _page(request)


def _upload(request):
    """Upload a file of one kind: a new run, on its column choice. The kind is checked by services.upload (403 for one the user may
    not import); a file it cannot read, or one too large, comes back to the page with the reason beside that kind's form."""
    kind = request.POST.get("kind", "")
    upload = request.FILES.get("file")
    try:
        if upload is None:
            if not imp_perms.can_import(request.user, kind):
                raise PermissionDenied
            raise ValidationError("Choose the CSV file to upload.")
        run = services.upload(request.user, kind, upload.name, upload.read(), max_rows=base.MAX_ROWS, max_bytes=base.MAX_BYTES)
    except ValidationError as e:
        return _page(request, error=" ".join(e.messages), error_kind=kind)
    return redirect(_run_url(run))


@web_view(*PAGE)
def import_template(request, kind):
    """A kind's template: its columns' names (each one of its aliases, so the template maps itself) and an example row."""
    importer = kinds.get(kind)
    if importer is None:
        raise Http404
    if not imp_perms.can_import(request.user, kind):
        raise PermissionDenied
    header, example = importer.template()
    return csv_response(f"{kind}-template.csv", header, [example])


# --- one run --------------------------------------------------------------------------------------

def _mapping_ctx(run, typed: dict | None = None, error: str = "") -> dict:
    """The column choice: the guess (services.mapping_view), or after a refused choice what the person chose, with its samples."""
    view = services.mapping_view(run)
    width = len(run.header)
    rows = []
    for item in view["columns"]:
        column, index, samples = item["column"], item["index"], item["samples"]
        if typed is not None:
            index = _index(typed.get(column.key), width)
            samples = base.samples(run.rows, index) if index is not None else []
        rows.append({"column": column, "index": index, "samples": samples})
    unused = view["unused"]
    if typed is not None:
        chosen = {r["index"] for r in rows}
        unused = [h for i, h in enumerate(run.header) if i not in chosen and h.strip()]
    return {"mapping": rows, "header": view["header"], "unused": unused, "duplicates": view["duplicates"], "map_error": error,
            "map_typed": typed is not None, "matched": sum(1 for r in rows if r["index"] is not None)}


def _progress_ctx(run, *, poll: bool, wait: bool = False) -> dict:
    return {"run": run, "pct": services.progress(run), "importing": run.status == Status.IMPORTING, "poll": poll, "trigger": WAIT if wait else "load",
            "done_text": f"{run.offset:,}", "rows_text": f"{run.row_count:,}"}


def _summary_ctx(run) -> dict:
    """What the check found, or what the import did: the counts, the notes grouped, and the importer's summary (each value read
    once with its rows, the totals, and the names it adds: services.CREATED_SHOWN of a group at most, which `capped` says)."""
    summary = run.summary or {}
    values = [{"column": column, "rows": [{"value": value, "shown": shown, "count_text": f"{count:,}"}
                                          for value, (shown, count) in sorted(found.items(), key=lambda item: (-item[1][1], item[0]))]}
              for column, found in summary.get("values", {}).items()]
    totals = [{"group": group, "rows": [(label, _amount(amount)) for label, amount in labels.items()]} for group, labels in summary.get("totals", {}).items()]
    created = [{"group": group, "first": names[:NAMES_SHOWN], "rest": names[NAMES_SHOWN:], "count": f"{len(names):,}",
                "capped": len(names) >= services.CREATED_SHOWN} for group, names in summary.get("created", {}).items()]
    groups = services.problem_groups(run)
    for g in groups:
        g["more"] = g["count"] - len(g["lines"])
        g["count_text"], g["more_text"] = f"{g['count']:,}", f"{g['more']:,}"
    counts = _counts(run)
    to_import, skipped = sum(c["n"] for c in counts if c["key"] in ("create", "update")), run.counts.get("skip", 0)
    started = _import_started(run)
    return {"counts": counts, "groups": groups, "values": values, "totals": totals, "created": created, "started": started,
            # a check stopped part way counted only the rows it reached: no counts, which would read as the file's
            "show_counts": bool(run.counts) and (started or run.checked_at is not None),
            "to_import": to_import, "to_import_text": f"{to_import:,}", "skipped": skipped, "skipped_text": f"{skipped:,}",
            "results_capped": len(run.results) >= services.RESULTS_KEPT, "results_kept": f"{services.RESULTS_KEPT:,}"}


def _run_page(request, run, *, error: str = "", typed: dict | None = None):
    importer = kinds.get(run.kind)
    ctx = {"nav_active": "settings", "run": run, "importer": importer, "css": STATUS_CSS.get(run.status, "neutral"), "error": error,
           "key_label": importer.column(importer.key).label, "expire_days": services.EXPIRE_DAYS}
    if run.status == Status.MAPPING:
        ctx.update(_mapping_ctx(run, typed, error))
    elif run.status in (Status.CHECKING, Status.IMPORTING):
        # The check carries on by itself; an import does only while someone is moving it (another tab, or this one a moment ago).
        ctx.update(_progress_ctx(run, poll=run.status == Status.CHECKING or timezone.now() - run.updated_at < STALLED))
    else:
        ctx.update(_summary_ctx(run))
    return render(request, "web/imports_run.html", ctx)


@web_view(*PAGE)
def import_run(request, pk):
    services.expire_stale()
    return _run_page(request, _run(request, pk))


@web_view(*PAGE)
def import_samples(request, pk):
    """The sample values of the file column a select now names (the column choice asks when it changes)."""
    run = _run(request, pk)
    if not request.htmx or run.status != Status.MAPPING:
        return redirect(_run_url(run))
    chosen = next((value for name, value in request.GET.items() if name.startswith("map-")), "")
    index = _index(chosen, len(run.header))
    return render(request, "web/_imports_samples.html", {"samples": base.samples(run.rows, index) if index is not None else []})


@require_POST
@web_view(*PAGE)
def import_columns(request, pk):
    """Confirm which file column feeds each of the importer's, and start the check. A refused choice comes back as chosen."""
    run = _step_run(pk)
    importer = kinds.get(run.kind)
    typed = {c.key: request.POST.get(f"map-{c.key}", "") for c in importer.columns} if importer else {}
    try:
        services.confirm_columns(run, request.user, {key: (value if value != "" else None) for key, value in typed.items()})
    except ValidationError as e:
        run = _run(request, pk)
        if run.status != Status.MAPPING:  # chosen already (another tab): the page says where the run is
            return redirect(_run_url(run))
        return _run_page(request, run, error=" ".join(e.messages), typed=typed)
    return redirect(_run_url(run))


@require_POST
@web_view(*PAGE)
def import_next(request, pk):
    """The next chunk of the current pass (services.process). The progress bar asks for it and gets the bar back until the pass is
    done; then the browser loads the run's page, which shows the result. Without JavaScript, Continue does one chunk a click."""
    run = _step_run(pk)
    before = (run.status, run.offset)
    run = services.process(run, request.user)
    if not request.htmx:
        return redirect(_run_url(run))
    if run.status not in (Status.CHECKING, Status.IMPORTING):
        return HttpResponseClientRedirect(_run_url(run))
    waited = (run.status, run.offset) == before  # another request holds the run and is doing this chunk: ask again in a moment
    return render(request, "web/_imports_progress.html", _progress_ctx(run, poll=True, wait=waited))


@require_POST
@web_view(*PAGE)
def import_start(request, pk):
    """Import a checked file (services.start_import: one import at a time per facility), then the progress."""
    run = _step_run(pk)
    try:
        services.start_import(run, request.user)
    except ValidationError as e:
        run = _run(request, pk)
        if run.status != Status.CHECKED:  # importing already (another tab), or ended: the page says where the run is
            return _go(request, _run_url(run))
        return _run_page(request, run, error=" ".join(e.messages))  # another import is running in the facility
    return _go(request, _run_url(run))


@require_POST
@web_view(*PAGE)
def import_discard(request, pk):
    """End a run that is not done (services.discard): its rows are cleared; an import under way keeps the chunks it committed."""
    run = _step_run(pk)
    try:
        services.discard(run, request.user)
    except ValidationError:
        pass  # it has ended already (another tab): its page says how
    return _go(request, _run_url(run))


@web_view(*PAGE)
def import_notes(request, pk):
    """Every row with a note, as the check or the import left them: line, key, outcome, and the notes (never a cell's free text)."""
    run = _run(request, pk)
    importer = kinds.get(run.kind)
    words = {key: (done if _import_started(run) else ahead) for key, ahead, done in OUTCOMES}
    rows = [[line, key, words.get(outcome, outcome), "; ".join(notes)] for line, key, outcome, notes in run.results]
    day = timezone.localtime(run.created_at).date()
    return csv_response(f"cadence-import-{run.kind}-notes-{day:%Y-%m-%d}.csv", ["Line", importer.column(importer.key).label, "Outcome", "Notes"], rows)
