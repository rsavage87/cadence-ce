"""
CSV downloads (Reports, and the Equipment, Work orders, and Contracts exports of slice 11).

One writer for all of them, so every file opens the same way in Excel:
- UTF-8 with a byte-order mark (Excel otherwise reads accented names as mojibake);
- dates as ISO (2026-09-30), money and other decimals as plain numbers, None as an empty cell;
- text that a spreadsheet would run as a formula (starting with =, +, -, @, tab, or carriage return) is prefixed with an
  apostrophe. Device names, vendor names, and problem text come from users and the FDA feed, so a cell like
  "=HYPERLINK(...)" must stay text. Numbers are never prefixed: a negative number is a number, not a formula.
"""
import csv
from datetime import date, datetime
from decimal import Decimal

from django.http import StreamingHttpResponse

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


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
        return value.isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


class _Echo:
    def write(self, value):
        return value


def csv_response(filename: str, columns: list[str], rows) -> StreamingHttpResponse:
    """Stream `rows` (an iterable of sequences) under `columns` as a download named `filename`."""
    writer = csv.writer(_Echo())

    def lines():
        yield "﻿" + writer.writerow([cell(c) for c in columns])
        for row in rows:
            yield writer.writerow([cell(v) for v in row])

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
