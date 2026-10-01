"""Reading CSV downloads in tests: they stream, and start with a byte-order mark so Excel reads UTF-8 (apps/web/exports.py)."""
import csv
import io


def csv_text(response) -> str:
    return b"".join(response.streaming_content).decode("utf-8-sig")


def csv_rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text(response))))
