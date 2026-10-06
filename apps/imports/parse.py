"""
Reading the values other CMMS exports write (slice 23). Every reader takes the cell's text and returns the value, or raises
Unreadable with words for the person fixing the file: a value the importer cannot read is never quietly turned into a default
(a date read as nothing would put every device's PM due today; a cost read as zero would hide spend). Blank cells are None.
"""
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class Unreadable(ValueError):
    """The cell has a value, and it is not one this column takes."""


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                "%Y%m%d")
MONTH_FORMATS = ("%Y-%m", "%m/%Y", "%b %Y", "%B %Y", "%b-%Y", "%b-%y")
# Dates systems write to mean "none": the epochs of Excel and Lotus, and far-future "never" placeholders.
PLACEHOLDER_DATES = {date(1899, 12, 30), date(1899, 12, 31), date(1900, 1, 1), date(9999, 12, 31), date(2099, 12, 31), date(2099, 1, 1)}
EXCEL_EPOCH = date(1899, 12, 30)
_TIME_SUFFIX = re.compile(r"[T ]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AaPp][Mm])?\s*(Z|[+-]\d{2}:?\d{2})?$")


class Placeholder(Unreadable):
    """A date that means "none" in the system it came from (1/1/1900, 12/31/2099): read as blank, with a note."""


def text(value: str) -> str:
    return (value or "").strip()


def parse_date(value: str, *, month_end: bool = False) -> date | None:
    """A date in any of the forms exports use: ISO, US (with 2- or 4-digit years), "15-Jan-2024", "Jan 15, 2024", a trailing time
    of day (report writers add "00:00:00"), or an Excel serial number (a date column Excel saved as a number). With `month_end`, a
    month alone ("2024-03", "Mar 2024") is that month's last day (a PM scheduled for a month). Raises Placeholder for a "none"
    date and Unreadable for anything else it cannot read."""
    s = text(value)
    if not s:
        return None
    s = _TIME_SUFFIX.sub("", s).strip()
    found = None
    if s.isdecimal() and len(s) == 5 and 20000 <= int(s) <= 60000:  # an Excel serial: 1954 to 2064
        found = EXCEL_EPOCH + timedelta(days=int(s))
    else:
        for fmt in DATE_FORMATS:
            try:
                found = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    if found is None and month_end:
        for fmt in MONTH_FORMATS:
            try:
                first = datetime.strptime(s, fmt).date()
            except ValueError:
                continue
            found = first.replace(day=monthrange(first.year, first.month)[1])
            break
    if found is None:
        raise Unreadable(f"{value.strip()!r} is not a date it can read (use YYYY-MM-DD or MM/DD/YYYY)")
    if found in PLACEHOLDER_DATES:
        raise Placeholder(f"{value.strip()!r} reads as no date")
    return found


def parse_decimal(value: str, *, places: int = 2, low: Decimal | None = None, high: Decimal | None = None, what: str = "number") -> Decimal | None:
    """A number, with thousands separators, a currency sign or code, and accounting negatives ("(1,250.00)"). Rounded half up to
    `places`; outside low..high is Unreadable rather than cut."""
    s = text(value)
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").replace("USD", "").replace("US$", "").strip()
    try:
        number = Decimal(s)
    except InvalidOperation:
        raise Unreadable(f"{value.strip()!r} is not a {what}") from None
    if not number.is_finite() or abs(number) >= Decimal(10) ** 15:  # beyond any column (and what quantize can hold)
        raise Unreadable(f"{value.strip()!r} is not a {what}")
    number = (-number if negative else number).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if (low is not None and number < low) or (high is not None and number > high):
        raise Unreadable(f"{value.strip()!r} is outside what a {what} can be here")
    return number


def parse_money(value: str, *, high: Decimal = Decimal("9999999999.99")) -> Decimal | None:
    """An amount of money: 0 or more, to the cent (numeric(12, 2) holds less than ten billion)."""
    return parse_decimal(value, low=Decimal(0), high=high, what="money amount")


YES = {"yes", "y", "true", "t", "1", "x"}
NO = {"no", "n", "false", "f", "0"}


def parse_yes_no(value: str) -> bool | None:
    s = text(value).lower()
    if not s:
        return None
    if s in YES:
        return True
    if s in NO:
        return False
    raise Unreadable(f"{value.strip()!r} is not yes or no")


INTERVAL_WORDS = {"annual": 12, "annually": 12, "yearly": 12, "semi-annual": 6, "semiannual": 6, "semi annual": 6, "semi-annually": 6,
                  "biannual": 6, "quarterly": 3, "monthly": 1, "bimonthly": 2, "bi-monthly": 2, "biennial": 24, "biennially": 24,
                  "every 2 years": 24, "triennial": 36}
_INTERVAL = re.compile(r"^(\d{1,3})\s*(m|mo|mos|mon|month|months|y|yr|yrs|year|years)?$")


def parse_interval_months(value: str) -> int | None:
    """A PM interval in months, 1 to 120: "12", "6 mo", "2 yr", "Semi-annual", "Quarterly"."""
    s = text(value).lower().replace(".", "")
    if not s:
        return None
    months = INTERVAL_WORDS.get(s)
    if months is None:
        m = _INTERVAL.match(s)
        if m:
            months = int(m.group(1)) * (12 if (m.group(2) or "m").startswith("y") else 1)
    if months is None or not 1 <= months <= 120:
        raise Unreadable(f"{value.strip()!r} is not a PM interval (1 to 120 months, or a word such as annual or quarterly)")
    return months


def parse_choice(value: str, words: dict[str, str], *, what: str) -> str | None:
    """One of a column's values, by the words exports use for it (`words`: lowercased word -> stored slug)."""
    s = " ".join(text(value).lower().replace("_", " ").split())
    if not s:
        return None
    if s in words:
        return words[s]
    raise Unreadable(f"{value.strip()!r} is not a {what} it knows ({', '.join(sorted(set(words.values())))})")
