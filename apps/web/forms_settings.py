"""
Settings screen input parsing (slice 8). Plain helpers: they turn the POSTed form into keyword arguments for
apps.facility.services.update_settings, which does all the validation. Nothing here saves anything.
"""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.facility.services import POLICY_FIELDS, TARGET_FIELDS, TARGET_RANGES


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


_THOUSANDS = re.compile(r"\d{1,3}(,\d{3})+(\.\d+)?")


def _number(field: str, value: str) -> str:
    """Tolerate what people paste into a number box: "$52,000" for the budget, "95 %" for a percentage. A comma counts only as a
    thousands separator in the budget ("52,000"); anything else ("1,5" meaning 1.5) goes to the service unchanged and is refused
    there, so a decimal comma never silently becomes a number ten times larger."""
    text = str(value or "").strip()
    if field == "repair_budget_monthly":
        text = text.removeprefix("$").strip()
        if _THOUSANDS.fullmatch(text):
            text = text.replace(",", "")
    elif field.endswith("_pct"):
        text = text.removesuffix("%").strip()
    return text


def portal_fields(post) -> dict:
    """The portal rows present in the post (each control saves only itself). The toggle posts a hidden 0 with the checkbox's 1
    when it is ticked, so on means a 1 is among the values, whatever their order. Anything else goes to the service to reject."""
    fields = {}
    if "portal_require_callback" in post:
        values = post.getlist("portal_require_callback") if hasattr(post, "getlist") else [post["portal_require_callback"]]
        fields["portal_require_callback"] = True if "1" in values else False if values == ["0"] else post.get("portal_require_callback")
    for field in ("portal_hotline", "portal_confirmation", "portal_email_domains"):
        if field in post:
            fields[field] = post.get(field, "")
    return fields


def policy_fields(post) -> dict:
    """All eight policy lines; a missing one counts as blank, which the service refuses."""
    return {field: post.get(field, "") for field in POLICY_FIELDS}


def target_fields(post) -> dict:
    return {field: _number(field, post.get(field, "")) for field in TARGET_FIELDS}


def error_dict(e: ValidationError) -> dict:
    """field -> first message, in the order the service found them (form order). A non-field error keys on "__all__"."""
    if hasattr(e, "error_dict"):
        return {field: messages[0] for field, messages in e.message_dict.items()}
    return {"__all__": e.messages[0]}
