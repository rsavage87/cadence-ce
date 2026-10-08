"""
Settings screen input parsing (slice 8). Plain helpers: they turn the POSTed form into keyword arguments for
apps.facility.services.update_settings, which does all the validation. Nothing here saves anything.
"""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.facility.models import PmWindow
from apps.facility.services import POLICY_FIELDS, RATE_FIELDS, RATE_MAX, TARGET_FIELDS, TARGET_RANGES, WINDOW_GROUPS


def plain_number(value) -> str:
    """A Decimal as a person would type it: 95.0 -> "95", 99.50 -> "99.5", 52000.00 -> "52000"; None -> ""."""
    if value is None:
        return ""
    d = Decimal(value).normalize()
    return format(d, "f")


# The KPI targets form, in screen order: field, label, help line. Ranges come from the service so the help never drifts.
_RANGE = {field: (low, high) for field, _label, low, high in TARGET_RANGES}


def _between(field, unit=""):
    low, high = _RANGE[field]
    return f"Between {plain_number(low)} and {plain_number(high)}{unit}"


TARGET_FORM = [
    ("target_pm_pct", "PM completion target (%)", "Medium and low risk equipment; life support and high risk are held at 100%"),
    ("target_uptime_pct", "Fleet uptime target (%)", _between("target_uptime_pct")),
    ("target_mttr_days", "Mean time to repair target (days)", _between("target_mttr_days", " days")),
    ("repair_budget_monthly", "Monthly repair budget ($)", "Shown on the Overview's spend tile; leave blank for none"),
]

# The labor rates (slice 15), in the same panel and form: field, label, help line. A change prices labor logged afterwards only.
_RATE_RANGE = f"Between 0 and {RATE_MAX:,}"
RATE_FORM = [
    ("labor_rate", "In-house labor rate ($ per hour)", f"{_RATE_RANGE}. Charged on time technicians log on work orders"),
    ("vendor_labor_rate", "Vendor labor rate ($ per hour)", f"{_RATE_RANGE}. Charged on vendor service time"),
]


_THOUSANDS = re.compile(r"\d{1,3}(,\d{3})+(\.\d+)?")


def _number(field: str, value: str) -> str:
    """Tolerate what people paste into a number box: "$52,000" for the budget or "$215" for a labor rate, "95 %" for a percentage.
    A comma counts only as a thousands separator in money ("52,000"); anything else ("1,5" meaning 1.5) goes to the service
    unchanged and is refused there, so a decimal comma never silently becomes a number ten times larger."""
    text = str(value or "").strip()
    if field == "repair_budget_monthly" or field in RATE_FIELDS:
        text = text.removeprefix("$").strip()
        if _THOUSANDS.fullmatch(text):
            text = text.replace(",", "")
    elif field.endswith("_pct"):
        text = text.removesuffix("%").strip()
    return text


def _toggle(post, field):
    """An on/off switch as posted: a hidden 0 with the checkbox's 1 when it is ticked, so on means a 1 is among the values, whatever
    their order, and off is the 0 alone. Anything else (or nothing) goes to the service to reject."""
    values = post.getlist(field) if hasattr(post, "getlist") else [post[field]] if field in post else []
    return True if "1" in values else False if values == ["0"] else post.get(field)


def portal_fields(post) -> dict:
    """The portal rows present in the post (each control saves only itself); the callback toggle as _toggle reads it."""
    fields = {}
    if "portal_require_callback" in post:
        fields["portal_require_callback"] = _toggle(post, "portal_require_callback")
    for field in ("portal_hotline", "portal_confirmation", "portal_email_domains"):
        if field in post:
            fields[field] = post.get(field, "")
    return fields


def policy_fields(post) -> dict:
    """All eight policy lines; a missing one counts as blank, which the service refuses."""
    return {field: post.get(field, "") for field in POLICY_FIELDS}


def target_fields(post) -> dict:
    return {field: _number(field, post.get(field, "")) for field in TARGET_FIELDS}


def rate_fields(post) -> dict:
    """The labor rates present in the post ("$215", "1,250.00" tolerated as for the budget). A rate the post leaves out is left
    as it is; one sent blank goes to the service, which refuses it."""
    return {field: _number(field, post.get(field, "")) for field in RATE_FIELDS if field in post}


def take_work_field(post) -> dict:
    """Slice 24: the Taking work toggle (technicians may take unassigned work they are credentialed for), as _toggle reads it. Always
    present, so a post without it is refused by the service rather than saving nothing."""
    return {"technicians_take_work": _toggle(post, "technicians_take_work")}


# The PM completion window (slice 27): each group's key (apps.facility.services.WINDOW_GROUPS) and its label, in screen order. The
# labels are the maintenance policy's for the two PM lines, so the panel and the policy name the groups alike.
PM_WINDOW_GROUPS = [("high", "Life support and high risk"), ("other", "Medium and low risk")]


def pm_window_fields(post) -> dict:
    """Slice 27: both groups' windows, as posted. A kind missing from the post counts as blank, which the service refuses (the panel
    always sends both). The days go only with "Within a number of days after the due date", as typed (blank included: the service
    asks for them); with any other kind they are None, so a number left in the hidden days box never rides along and is refused."""
    fields = {}
    for kind_field, days_field in WINDOW_GROUPS.values():
        kind = post.get(kind_field, "")
        fields[kind_field] = kind
        fields[days_field] = post.get(days_field, "").strip() if kind == PmWindow.DAYS_AFTER else None
    return fields


def time_zone_field(post) -> str:
    """The time zone chosen (slice 21), as posted; the service decides whether it is one (blank or unknown is refused there)."""
    return post.get("time_zone", "")


def error_dict(e: ValidationError) -> dict:
    """field -> first message, in the order the service found them (form order). A non-field error keys on "__all__"."""
    if hasattr(e, "error_dict"):
        return {field: messages[0] for field, messages in e.message_dict.items()}
    return {"__all__": e.messages[0]}
