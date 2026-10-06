"""
The pieces every importer shares (slice 23): reading an uploaded file into a header and rows, matching its columns to an importer's
by name, and the Importer base class a kind (apps.imports.kinds) fills in. The steps that run them are apps.imports.services.

Reading refuses what PostgreSQL or the person would regret: a workbook (.xlsx, .xls) instead of a CSV, text with NUL characters,
cells too long for any column, and more rows than a run takes. UTF-8 (with or without the byte-order mark Excel writes), UTF-16 and
UTF-32 by their byte-order marks, and Windows-1252 (what Excel's plain "CSV" writes in the US) are read; commas, semicolons, tabs,
and pipes separate columns. Every cell is trimmed and loses the apostrophe Cadence's own CSVs put before formula-like text.
"""
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.core.csvtext import unguard

MAX_BYTES = 5 * 1024 * 1024        # a file on the screen
MAX_ROWS = 20_000                  # rows in one run on the screen; import_data has no limit
MAX_CELL = 4_000                   # characters in one cell: longer than any column, so never a real value
SAMPLES = 3                        # sample values shown per column on the mapping step

_BOMS = ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"), (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"),
         (b"\xfe\xff", "utf-16"))
_WORKBOOKS = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")  # .xlsx (a zip) and .xls (an OLE file)


def decode(data: bytes) -> str:
    """The file's text. Raises ValidationError with words for the person when it is not a readable CSV."""
    if data.startswith(_WORKBOOKS):
        raise ValidationError("This is an Excel workbook, not a CSV. In Excel, choose File, Save As, \"CSV UTF-8 (Comma delimited)\", and upload that file.")
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                result = data.decode(encoding)
            except UnicodeDecodeError:
                raise ValidationError("The file's text could not be read. Save it as \"CSV UTF-8\" and upload it again.") from None
            break
    else:
        try:
            result = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                result = data.decode("cp1252")
            except UnicodeDecodeError:
                raise ValidationError("The file's text could not be read. Save it as \"CSV UTF-8\" and upload it again.") from None
    if "\x00" in result:
        line = result[:result.index("\x00")].count("\n") + 1
        raise ValidationError(f"Line {line} holds a NUL character, which a CSV never has: the file may be damaged or not a CSV.")
    return result


def read_table(text: str, *, max_rows: int | None = MAX_ROWS) -> tuple[list[str], list[list[str]]]:
    """The header and the rows (each padded or cut to the header's width; blank rows left out), streamed so a file of blank lines
    never builds a huge list. Raises ValidationError for an unreadable file, a cell over MAX_CELL, or more than `max_rows` rows."""
    first_line = text.split("\n", 1)[0]
    delimiter = max(",;\t|", key=first_line.count) if any(d in first_line for d in ",;\t|") else ","  # the header's separator
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader, None)
        if not header or not any(h.strip() for h in header):
            raise ValidationError("The file has no header row: the first line must name the columns.")
        header = [h.strip() for h in header]
        width = len(header)
        rows = []
        for cells in reader:
            if not any(c.strip() for c in cells):
                continue
            if any(len(c) > MAX_CELL for c in cells):
                raise ValidationError(f"Line {reader.line_num} has a cell longer than {MAX_CELL:,} characters; no column holds that much.")
            if max_rows is not None and len(rows) >= max_rows:
                raise ValidationError(f"The file has more than {max_rows:,} rows. Split it (by year, or by department) and import the parts in turn.")
            rows.append([c for c in (cells + [""] * width)[:width]])
    except csv.Error as e:
        raise ValidationError(f"Line {reader.line_num} could not be read as CSV ({e}).") from None
    if not rows:
        raise ValidationError("The file has a header but no rows.")
    return header, rows


def clean(cell: str) -> str:
    """A cell as the importers read it: trimmed, without the apostrophe Cadence's exports put before formula-like text."""
    return unguard((cell or "").strip()).strip()


def normalize(name: str) -> str:
    """A column name compared with aliases: lowercase, single spaces, no surrounding punctuation ("Control No." -> "control no")."""
    return " ".join(re.sub(r"[_\s]+", " ", (name or "").lower()).split()).strip(" .:")


@dataclass(frozen=True)
class Column:
    """One value an importer reads. `aliases` are the names exports give it (compared after normalize()); the first one is the name
    in the template file. `required`: a new record cannot be made without it. `max_length`: the model field's, checked before any
    service runs (PostgreSQL would refuse a longer value). `example` goes in the template's example row."""

    key: str
    label: str
    aliases: tuple[str, ...]
    required: bool = False
    max_length: int | None = None
    help: str = ""
    example: str = ""


def auto_map(columns: list[Column], header: list[str]) -> dict[str, int]:
    """Column key -> the header's index, by alias (the first matching header wins). Unmatched columns are left out."""
    names = [normalize(h) for h in header]
    mapping = {}
    for column in columns:
        for alias in column.aliases:
            if alias in names:
                mapping[column.key] = names.index(alias)
                break
    return mapping


def duplicate_headers(header: list[str]) -> list[str]:
    seen = Counter(normalize(h) for h in header if h.strip())
    return sorted({h for h in header if h.strip() and seen[normalize(h)] > 1})


def samples(rows: list[list[str]], index: int) -> list[str]:
    """The first SAMPLES different non-blank values of a column, shortened for the screen."""
    found = []
    for row in rows:
        value = clean(row[index]) if index < len(row) else ""
        if value and value not in found:
            found.append(value[:60])
            if len(found) == SAMPLES:
                break
    return found


class RowSkip(Exception):
    """Raised by an importer: this row is not imported, for this reason (shown to the person)."""


@dataclass
class RowResult:
    """What became of one row: created, updated, unchanged, or skipped, and the notes for the person (warnings on a row that was
    imported, the reason on one that was not). Notes never repeat a cell's free text: a key and words."""

    line: int
    key: str
    outcome: str = "unchanged"
    notes: list[str] = field(default_factory=list)

    CREATE, UPDATE, UNCHANGED, SKIP = "create", "update", "unchanged", "skip"

    def warn(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)

    def as_list(self) -> list:
        return [self.line, self.key, self.outcome, self.notes]


@dataclass
class Context:
    """One chunk's work: who imports (None from the command line, which may do anything), the facility's today, whether this is
    the check (everything is rolled back afterwards) or the import, a place for an importer's lookups, and the chunk's part of the
    run's summary, which the services merge into the run: totals ("Work orders by year", "2023") added up, names the import adds
    ("Departments": "ICU"), and how a choice column's values were read ("Status": "Disposed" -> "Retired")."""

    user: object
    today: object
    check: bool
    cache: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    created: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)

    def total(self, group: str, label: str, amount=1) -> None:
        bucket = self.totals.setdefault(group, {})
        bucket[label] = str(Decimal(bucket.get(label, "0")) + Decimal(amount))

    def add_created(self, group: str, name: str) -> None:
        names = self.created.setdefault(group, [])
        if name not in names:
            names.append(name)

    def read_as(self, column: str, value: str, shown: str) -> None:
        """Record that `value` in `column` was read as `shown` (the screen shows each distinct value once, with a count)."""
        bucket = self.values.setdefault(column, {})
        entry = bucket.setdefault(value, [shown, 0])
        entry[1] += 1


class Importer:
    """One kind of file. A kind declares its columns and the natural key that makes a re-run safe (rows already in Cadence are
    found by it, never doubled), and applies one row at a time through the services, raising RowSkip (or letting a service's
    ValidationError or PermissionDenied through) to skip the row. The services run every row in a savepoint and catch those, and
    a database refusal too, so one bad row never stops the file."""

    kind = ""
    label = ""                 # "Devices"
    module = ""                # apps.accounts.models.Module: whose level importing needs
    level = 0                  # apps.accounts.models.Level
    key = ""                   # the column whose value names a row's record
    description = ""           # a sentence for the screen: what the file holds and what the import does
    order = 0                  # where it comes in an onboarding: devices first, then contracts, technicians, work orders
    columns: list[Column] = []

    def column(self, key: str) -> Column:
        return next(c for c in self.columns if c.key == key)

    def required(self) -> list[Column]:
        return [c for c in self.columns if c.required]

    def prepare(self, rows: list[dict]) -> dict[int, str]:
        """Problems of the file as a whole, by row index, found before any row is applied: by default a key that comes more than
        once (case-insensitively), which a check run in chunks would otherwise not see. Subclasses may add their own."""
        firsts, problems = {}, {}
        for i, row in enumerate(rows):
            value = (row.get(self.key) or "").strip().lower()
            if not value:
                continue
            if value in firsts:
                problems[i] = f"{self.column(self.key).label} {row[self.key]} is also on line {firsts[value] + 2}: one row per record"
            else:
                firsts[value] = i
        return problems

    def load(self, ctx: Context, rows: list[dict]) -> None:
        """Look up, once for the chunk, what its rows will need (ctx.cache)."""

    def apply(self, ctx: Context, row: dict, result: RowResult) -> None:
        raise NotImplementedError

    def template(self) -> tuple[list[str], list[str]]:
        """The template file's header (each column's label, which is always one of its aliases, so the template maps itself) and its
        example row."""
        return [c.label for c in self.columns], [c.example for c in self.columns]
