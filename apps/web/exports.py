"""
CSV downloads (Reports, and the Equipment, Work orders, and Contracts exports of slice 11).

One writer for all of them, so every file opens the same way in Excel:
- UTF-8 with a byte-order mark (Excel otherwise reads accented names as mojibake);
- dates as ISO (2026-09-30), times as ISO in the facility's zone (2026-10-04T22:30-10:00, never UTC or the server's), money and
  other decimals as plain numbers, None as an empty cell;
- text that a spreadsheet would run as a formula (starting with =, +, -, @, tab, or carriage return) is prefixed with an
  apostrophe. Device names, vendor names, and problem text come from users and the FDA feed, so a cell like
  "=HYPERLINK(...)" must stay text. Numbers are never prefixed: a negative number is a number, not a formula.

The rows are written while the response streams, which is after TenantMiddleware has reset the tenant (the ORM scope, and the
Postgres `app.tenant_id` that row-level security reads). A queryset read then would see no rows under RLS, so the writer works
inside the tenant that was current when the response was made, for as long as it writes rows; tenant_context also brings back
the facility's time zone (slice 21), which the middleware's has left by then too.
"""
import csv
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal

from django.http import StreamingHttpResponse
from django.utils import timezone

from apps.core.csvtext import FORMULA_START, guard  # noqa: F401  (FORMULA_START: the tests read it here)
from apps.tenants.context import get_current_tenant, tenant_context


def cell(value):
    """One value as the CSV shows it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        # An aware time (every one the database gives) in the zone at work: the facility's while its rows are written.
        return (timezone.localtime(value) if timezone.is_aware(value) else value).isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    # Inside the text too: Excel in many regions splits a double-clicked CSV on ";" (whatever the quoting, since this file is
    # comma-separated), so "Pump alarm;=WEBSERVICE(...)" would give a cell starting with "=" that Excel runs. An apostrophe
    # after a separator or line break keeps that piece text as well.
    return guard(str(value))


class _Echo:
    def write(self, value):
        return value


_writer = csv.writer(_Echo())


def csv_line(values) -> str:
    """One CSV line (CRLF, quoted where needed) from raw values."""
    return _writer.writerow([cell(v) for v in values])


def csv_response(filename: str, columns: list[str], rows) -> StreamingHttpResponse:
    """Stream `rows` (an iterable of sequences, such as a generator over a queryset) under `columns` as a download named `filename`."""
    tenant = get_current_tenant()

    def lines():
        yield "\ufeff" + csv_line(columns)  # the byte-order mark: Excel reads the file as UTF-8
        # Closing the response (a finished or dropped download) closes this generator, which leaves the tenant again.
        with tenant_context(tenant) if tenant is not None else nullcontext():
            for row in rows:
                yield csv_line(row)

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
