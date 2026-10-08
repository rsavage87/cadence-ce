"""
Facility settings (slice 8): the service request portal, maintenance policy text, KPI targets, the integration list,
and the risk-scoring summary the Settings screen shows. Views and the API call these; nothing sets fields directly.

Reads never write: get_settings() returns unsaved defaults until someone saves. Every save records who made it
(django-simple-history), because a change to policy or targets changes what surveyors and managers are shown.

Portal emails (slice 13): with the confirmation set to "On screen and by email", the portal may email a requester, but only at one
of the facility's own work email domains, so the public form can never be used to send mail anywhere else. at_domains() is the
one check: the portal form refuses another domain, and apps.portal.notifications asks email_allowed() again at sending time,
because the setting or the domains may have changed since the request came in.

The facility's time zone (slice 21): Tenant.timezone, which every request, job, and command inside the facility works in
(apps.tenants.context), so it decides the facility's today (what is overdue, due dates, contract days left, report periods), the
times shown, and when its daily jobs and emails run. set_time_zone() is the one writer: a zone this server knows, or a plain
refusal; the facility's own history records who changed it (the change log's Facility area, apps.core.history).

Taking work (slice 24): whether technicians may take open work nobody has, on devices they are credentialed for
(technicians_may_take; apps.workorders.services.take is the one place it happens). On until the facility turns it off.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.urls import reverse
from django.utils import timezone

from apps.equipment.models import Asset, RiskClass
from apps.tenants.context import get_current_tenant
from apps.tenants.models import Tenant

from .models import PM_WINDOW_DAYS_MAX, POLICY, POLICY_DEFAULTS, POLICY_MAX_LENGTH, FacilitySettings, PmWindow

PORTAL_FIELDS = ("portal_require_callback", "portal_hotline", "portal_confirmation", "portal_email_domains")
POLICY_FIELDS = tuple(field for field, _label, _default in POLICY)
TARGET_FIELDS = ("target_pm_pct", "target_uptime_pct", "target_mttr_days", "repair_budget_monthly")
# Labor rates (slice 15): what a labor line logged on a work order is charged at unless the line says otherwise. Lines keep the
# rate they were logged at, so a change here prices new lines only (apps.workorders.costs).
RATE_FIELDS = ("labor_rate", "vendor_labor_rate")
# Taking work (slice 24): whether technicians may take unassigned work they are credentialed for (apps.workorders.services.take).
TAKE_FIELDS = ("technicians_take_work",)
# Slice 27: the PM completion window (apps.pm.windows), a kind and its days for each group.
WINDOW_GROUPS = {"high": ("pm_window_high", "pm_window_high_days"), "other": ("pm_window_other", "pm_window_other_days")}
WINDOW_KINDS = tuple(kind for kind, _days in WINDOW_GROUPS.values())
WINDOW_DAYS = tuple(days for _kind, days in WINDOW_GROUPS.values())
WINDOW_FIELDS = WINDOW_KINDS + WINDOW_DAYS
EDITABLE = PORTAL_FIELDS + POLICY_FIELDS + TARGET_FIELDS + RATE_FIELDS + TAKE_FIELDS + WINDOW_FIELDS
TOGGLES = ("portal_require_callback", "technicians_take_work")  # on or off, and nothing else

# (field, label, low, high): the ranges a target may take. Life support and high risk PM stay at 100% (survey rule).
TARGET_RANGES = [("target_pm_pct", "PM completion target", Decimal("50"), Decimal("100")),
                 ("target_uptime_pct", "Fleet uptime target", Decimal("90"), Decimal("100")),
                 ("target_mttr_days", "Mean time to repair target", Decimal("0.5"), Decimal("30"))]
LIFE_SUPPORT_PM_TARGET = 100.0
HOTLINE_MAX_LENGTH = 40
BUDGET_MAX = Decimal("9999999999.99")  # the largest value the DecimalField(12, 2) column holds
RATE_LABELS = {"labor_rate": "In-house labor rate", "vendor_labor_rate": "Vendor labor rate"}
RATE_MAX = Decimal("9999.99")  # per hour; also the ceiling for a rate set on one labor line

# Confirmation to the requester: on screen always, and by email when the facility turns it on. Text messages are not offered.
CONFIRM_SCREEN, CONFIRM_EMAIL = "screen", "email"
CONFIRMATION_CHOICES = list(FacilitySettings._meta.get_field("portal_confirmation").choices)
EMAIL_DOMAINS_MAX = 10
EMAIL_DOMAINS_MAX_LENGTH = FacilitySettings._meta.get_field("portal_email_domains").max_length
# A plain domain: dot-separated labels of letters, digits, and inner hyphens, ending in a label that starts with a letter.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+[a-z][a-z0-9-]{{0,61}}[a-z0-9]")


def _places(field: str) -> int:
    """Decimal places the column keeps. More precise input is refused, not rounded, so what the user confirmed is what is stored."""
    return FacilitySettings._meta.get_field(field).decimal_places


def get_settings() -> FacilitySettings:
    """The tenant's settings, or unsaved defaults when it has never saved any. Requires a tenant context: without one the
    scoped manager finds nothing and this would quietly return defaults, so it refuses instead."""
    if get_current_tenant() is None:
        raise RuntimeError("get_settings() needs a tenant context")
    return FacilitySettings.objects.first() or FacilitySettings()


def decimal_places(d: Decimal) -> int:
    """Decimal places that carry a digit: 1.50 has 1, 2.000 has 0. Worked from the digits, never normalize(), which rounds a long
    number to the context's precision (82.000000000000000000000000000001 would pass as 82) and overflows on a huge exponent."""
    _sign, digits, exponent = d.as_tuple()
    if not isinstance(exponent, int) or exponent >= 0 or not any(digits):
        return 0
    places = -exponent
    for digit in reversed(digits):
        if digit != 0 or places == 0:
            break
        places -= 1
    return places


def _decimal(value, field: str, errors: dict):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        errors[field] = "Enter a number."
        return None
    if not d.is_finite():
        errors[field] = "Enter a number."
        return None
    places = _places(field)
    if decimal_places(d) > places:
        errors[field] = f"Use at most {places} decimal place{'' if places == 1 else 's'}."
        return None
    return abs(d) if d == 0 else d  # drops a negative zero's sign; no arithmetic, so a huge exponent reaches the range checks


def _text(value, field: str, errors: dict) -> str:
    """One line, stray whitespace collapsed. NUL characters are refused (Postgres cannot store them)."""
    text = " ".join(str(value or "").split())
    if "\x00" in text:
        errors[field] = "Remove the invisible control character from this text."
    return text


def _domain_problem(item: str) -> str:
    """Why `item` (lowercased, trimmed) is not a plain domain, or "" when it is one."""
    if "@" in item:
        return "Enter the part after the @ only, like riverside-health.org."
    if "/" in item or ":" in item:
        return "Enter the domain only, like riverside-health.org, without https:// or a slash."
    if any(c.isspace() for c in item):
        return "Separate domains with commas, like riverside-health.org, rrmc.org."
    if len(item) > 253 or not _DOMAIN.fullmatch(item):
        shown = f"{item} is" if len(item) <= 60 and item.isprintable() else "That is"
        return f"{shown} not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."
    return ""


def _domains(value, field: str, errors: dict) -> str:
    """Comma-separated work email domains: each a plain domain, lowercased, without repeats, at most EMAIL_DOMAINS_MAX.
    Stored as "a.org, b.org"; blank is allowed (the email option then cannot be chosen)."""
    found = []
    for part in str(value or "").split(","):
        item = part.strip().lower()
        if not item:
            continue
        problem = _domain_problem(item)
        if problem:
            errors[field] = problem
            return ""
        if item not in found:
            found.append(item)
    text = ", ".join(found)
    if len(found) > EMAIL_DOMAINS_MAX:
        errors[field] = f"List at most {EMAIL_DOMAINS_MAX} domains."
    elif len(text) > EMAIL_DOMAINS_MAX_LENGTH:
        errors[field] = f"Keep the domains to {EMAIL_DOMAINS_MAX_LENGTH} characters in all."
    return text


def _clean(fields: dict) -> dict:
    """Validate and normalize `fields`; returns the cleaned values or raises ValidationError (field -> message)."""
    errors, cleaned = {}, {}
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError(f"Unknown settings: {', '.join(sorted(unknown))}.")
    for field, value in fields.items():
        if field in TOGGLES:
            if not isinstance(value, bool):
                errors[field] = "Choose on or off."
            cleaned[field] = value
        elif field == "portal_hotline":
            text = _text(value, field, errors)
            if field not in errors and len(text) > HOTLINE_MAX_LENGTH:
                errors[field] = f"Keep the hotline to {HOTLINE_MAX_LENGTH} characters, e.g. ext. 4400."
            cleaned[field] = text
        elif field == "portal_confirmation":
            if value not in (CONFIRM_SCREEN, CONFIRM_EMAIL):
                errors[field] = "Choose On screen, or On screen and by email."
            cleaned[field] = value
        elif field == "portal_email_domains":
            cleaned[field] = _domains(value, field, errors)
        elif field in POLICY_FIELDS:
            text = _text(value, field, errors)
            if field in errors:
                pass
            elif not text:
                errors[field] = "Policy text cannot be empty. Use Reset to defaults to restore it."
            elif len(text) > POLICY_MAX_LENGTH:
                errors[field] = f"Keep each policy to {POLICY_MAX_LENGTH} characters."
            cleaned[field] = text
        elif field == "repair_budget_monthly":
            d = _decimal(value, field, errors)
            if d is not None and d < 0:
                errors[field] = "The budget cannot be negative."
            elif d is not None and d > BUDGET_MAX:
                errors[field] = "That budget is too large."
            cleaned[field] = d
        elif field in WINDOW_KINDS:
            if value not in PmWindow.values:
                errors[field] = "Choose when a PM counts as on time."
            cleaned[field] = value
        elif field in WINDOW_DAYS:
            cleaned[field] = _window_days(value, field, errors)
        elif field in RATE_FIELDS:
            d = _decimal(value, field, errors)
            label = RATE_LABELS[field]
            if d is None and field not in errors:
                errors[field] = f"{label} is required."
            elif d is not None and not (0 <= d <= RATE_MAX):
                errors[field] = f"{label} must be between $0 and ${RATE_MAX:,} an hour."
            cleaned[field] = d
        else:
            d = _decimal(value, field, errors)
            _f, label, low, high = next(r for r in TARGET_RANGES if r[0] == field)
            if d is None and field not in errors:
                errors[field] = f"{label} is required."
            elif d is not None and not (low <= d <= high):
                errors[field] = f"{label} must be between {low} and {high}."
            cleaned[field] = d
    if errors:
        raise ValidationError(errors)
    return cleaned


def _window_days(value, field: str, errors: dict):
    """A whole number of days, 1 to PM_WINDOW_DAYS_MAX, or None (blank): never True, 14.5, "1e1", or "14 days"."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit())):
        errors[field] = f"Enter a whole number of days, 1 to {PM_WINDOW_DAYS_MAX}."
        return None
    days = int(value)
    if not 1 <= days <= PM_WINDOW_DAYS_MAX:
        errors[field] = f"Enter a whole number of days, 1 to {PM_WINDOW_DAYS_MAX}."
    return days


def _check_window(cleaned: dict, s: FacilitySettings | None) -> None:
    """The window on the saved row with this change applied (slice 27): "days after the due date" needs its days (sent or saved); any
    other kind keeps none, so days sent with it, or sent alone while the saved kind takes none, are refused rather than silently
    dropped. Days a kind no longer uses are cleared. Mutates `cleaned`."""
    current = s or FacilitySettings()
    errors = {}
    for kind_field, days_field in WINDOW_GROUPS.values():
        if kind_field not in cleaned and days_field not in cleaned:
            continue
        kind = cleaned.get(kind_field, getattr(current, kind_field))
        days = cleaned[days_field] if days_field in cleaned else getattr(current, days_field)
        if kind == PmWindow.DAYS_AFTER:
            if days is None:
                errors[days_field] = f"Say how many days after the due date (1 to {PM_WINDOW_DAYS_MAX})."
        elif cleaned.get(days_field) is not None:
            errors[days_field] = "Days apply only to \"Within a number of days after the due date\"."
        else:
            cleaned[days_field] = None
    if errors:
        raise ValidationError(errors)


def _follow_window_in_policy(cleaned: dict, s: FacilitySettings | None) -> None:
    """When a group's window changes and its PM policy line is still the default text for the old window, the line follows the new
    window in the same save (the policy line and the measure never contradict each other by default); a line the facility wrote is
    left alone (the survey binder checks it against the window). Mutates `cleaned`."""
    from apps.pm import windows as pm_windows

    current = s or FacilitySettings()
    for group, (kind_field, days_field) in WINDOW_GROUPS.items():
        policy_field = pm_windows.PM_POLICY_FIELDS[group]
        if policy_field in cleaned or (kind_field not in cleaned and days_field not in cleaned):
            continue
        old = pm_windows.Window(getattr(current, kind_field), getattr(current, days_field))
        new = pm_windows.Window(cleaned.get(kind_field, old.kind), cleaned[days_field] if days_field in cleaned else old.days)
        if new != old and getattr(current, policy_field) == pm_windows.policy_default(group, old):
            cleaned[policy_field] = pm_windows.policy_default(group, new)


def policy_defaults(s: FacilitySettings | None = None) -> dict:
    """The eight policy lines' default texts, the two PM lines following the facility's window (slice 27)."""
    from apps.pm import windows as pm_windows

    w = pm_windows.windows(s or get_settings())
    out = dict(POLICY_DEFAULTS)
    for group, field in pm_windows.PM_POLICY_FIELDS.items():
        out[field] = pm_windows.policy_default(group, getattr(w, group))
    return out


def _locked_row():
    """The tenant's row, locked for this transaction (a no-op lock on SQLite), or None before the first save."""
    return FacilitySettings.objects.select_for_update().first()


def _check_portal_email(cleaned: dict, s: FacilitySettings | None) -> None:
    """Email confirmations need at least one work email domain, judged on the saved row with this change applied, so neither
    turning email on nor clearing the domains can leave the portal set to email with nowhere it may send."""
    if "portal_confirmation" not in cleaned and "portal_email_domains" not in cleaned:
        return
    current = s or FacilitySettings()
    confirmation = cleaned.get("portal_confirmation", current.portal_confirmation)
    domains = cleaned.get("portal_email_domains", current.portal_email_domains)
    if confirmation == CONFIRM_EMAIL and not domains:
        if current.portal_confirmation == CONFIRM_EMAIL:
            message = "Email confirmations need a work email domain. Choose On screen before removing the last one."
        else:
            message = "Add a work email domain before turning on email confirmations."
        raise ValidationError({"portal_confirmation": message})


def update_settings(by=None, **fields) -> FacilitySettings:
    """Change any of EDITABLE. All or nothing: one invalid field rejects the whole change and writes nothing. The row is
    locked while it changes, and two first saves racing each other both land on the one row (the second retries)."""
    cleaned = _clean(fields)
    with transaction.atomic():
        s = _locked_row()
        if s is None:
            _check_portal_email(cleaned, None)
            _check_window(cleaned, None)
            _follow_window_in_policy(cleaned, None)
            s = FacilitySettings(**cleaned)
            if by is not None:
                s._history_user = by
            try:
                with transaction.atomic():  # savepoint: a concurrent first save may win the unique-per-tenant row
                    s.save()
                return s
            except IntegrityError:
                s = FacilitySettings.objects.select_for_update().get()
        _check_portal_email(cleaned, s)
        _check_window(cleaned, s)
        _follow_window_in_policy(cleaned, s)
        for field, value in cleaned.items():
            setattr(s, field, value)
        if by is not None:
            s._history_user = by
        s.save()
    return s


def reset_policy(by=None) -> FacilitySettings:
    return update_settings(by=by, **policy_defaults())


def policy_items(s: FacilitySettings | None = None) -> list[dict]:
    """The eight policy lines for the screen: field, label, current text, default text (the PM lines' for the facility's window)."""
    s = s or get_settings()
    defaults = policy_defaults(s)
    return [{"field": field, "label": label, "text": getattr(s, field), "default": defaults[field], "is_default": getattr(s, field) == defaults[field]}
            for field, label, _default in POLICY]


def kpi_targets(s: FacilitySettings | None = None) -> dict:
    """What the Overview tiles and the PM trend line compare against, as floats."""
    s = s or get_settings()
    return {"pm_on_time": float(s.target_pm_pct), "pm_on_time_life_support": LIFE_SUPPORT_PM_TARGET, "uptime_pct": float(s.target_uptime_pct),
            "mttr_days": float(s.target_mttr_days),
            "repair_budget_monthly": float(s.repair_budget_monthly) if s.repair_budget_monthly is not None else None}


def labor_rates(s: FacilitySettings | None = None) -> dict:
    """The hourly rates new labor lines are charged at: in-house and vendor service, as Decimals."""
    s = s or get_settings()
    return {"in_house": s.labor_rate, "vendor": s.vendor_labor_rate}


def technicians_may_take(s: FacilitySettings | None = None) -> bool:
    """Whether technicians may take unassigned work they are credentialed for (slice 24; apps.workorders.services.take)."""
    return (s or get_settings()).technicians_take_work


def compliance_targets(s: FacilitySettings | None = None) -> dict:
    """PM compliance targets by risk class: 100% for life support and high risk (survey rule), the policy target otherwise."""
    pm = float((s or get_settings()).target_pm_pct)
    return {RiskClass.LIFE_SUPPORT: LIFE_SUPPORT_PM_TARGET, RiskClass.HIGH: LIFE_SUPPORT_PM_TARGET, RiskClass.MEDIUM: pm, RiskClass.LOW: pm}


def email_domains(s: FacilitySettings | None = None) -> list[str]:
    """The work email domains the portal may email, as saved (lowercase, in the order entered)."""
    s = s or get_settings()
    return [d for d in (part.strip().lower() for part in s.portal_email_domains.split(",")) if d]


def portal_emails_on(s: FacilitySettings | None = None) -> bool:
    """Whether the portal emails requesters now: the email option is chosen and there is a domain it may send to."""
    s = s or get_settings()
    return s.portal_confirmation == CONFIRM_EMAIL and bool(email_domains(s))


def at_domains(address: str, domains: list[str]) -> bool:
    """True when `address` is at one of `domains`, exactly (a subdomain is a different domain), in any letter case."""
    local, at, domain = (address or "").strip().lower().rpartition("@")
    return bool(local and at and domain in domains)


def email_allowed(address: str, s: FacilitySettings | None = None) -> bool:
    """True when the portal may email `address` now: emails are on and the address is at one of the facility's domains."""
    s = s or get_settings()
    return portal_emails_on(s) and at_domains(address, email_domains(s))


def domains_text(domains: list[str]) -> str:
    """["a.org"] -> "a.org"; two -> "a.org or b.org"; more -> "a.org, b.org, or c.org"."""
    if len(domains) <= 2:
        return " or ".join(domains)
    return f"{', '.join(domains[:-1])}, or {domains[-1]}"


def portal_url(tenant, department: str | None = None) -> str:
    """The public request form for this tenant; with a department, the link pre-fills it."""
    url = f"{django_settings.PORTAL_BASE_URL.rstrip('/')}{reverse('portal:request', args=[tenant.slug])}"
    return f"{url}?{urlencode({'dept': department})}" if department else url


def asset_request_url(asset) -> str:
    """The request form for one device, which pre-fills it: the device drawer's Request link and its asset label's QR code."""
    return f"{portal_url(asset.tenant)}?{urlencode({'asset': asset.tag})}"


# --- integrations --------------------------------------------------------------------------------------

CONNECTED, NOT_CONNECTED, LICENSE = "connected", "not_connected", "license"
INTEGRATIONS = [
    # key, short code, name, what it does (the mock's list; only the FDA feed is built)
    ("fda", "FDA", "FDA recalls", "Recall notices from the FDA, matched to device models in the inventory automatically"),
    ("ecri", "ECRI", "ECRI Alerts Tracker", "Hazard reports and recalls matched to the inventory"),
    ("oem", "OEM", "OEM PM library", "Standard PM procedures and intervals by manufacturer and model, with revision tracking"),
    ("ehr", "EHR", "EHR device association", "FHIR Device resources so patient-connected equipment appears in the chart and location"),
    ("rtls", "RTLS", "Real-time location", "Last-seen location for tagged mobile equipment, replacing manual room fields"),
    ("erp", "ERP", "Purchasing and ERP", "Parts purchase orders, receiving for incoming inspection, and capital requests"),
    ("sso", "SSO", "Single sign-on", "SAML login with role mapping from the hospital directory"),
    ("msg", "MSG", "Notifications", "Email and text for critical work orders, recall matches, and overdue life-support PMs"),
]


def _fda_status() -> tuple[str, str]:
    """Connected once the openFDA import has brought in a real notice. The demo seed's sample alerts (raw["demo"]) are
    fictional and never count as a connection. Alerts are global; the match count is this tenant's."""
    from apps.recalls.models import Alert, AlertMatch

    fda = Alert.objects.filter(source=Alert.Source.FDA)
    samples = fda.filter(raw__demo=True).values("pk")
    real = fda.exclude(pk__in=samples)
    at = real.aggregate(at=Max("created_at"))["at"]
    if at is None:
        return NOT_CONNECTED, "Only sample alerts so far; no FDA notices imported yet" if fda.exists() else "No FDA notices imported yet"
    at = timezone.localtime(at)
    matched = AlertMatch.objects.filter(alert__in=real).count()
    return CONNECTED, f"Newest notice imported {at:%b} {at.day}, {at.year} · {matched} matched to this inventory"


def integrations() -> list[dict]:
    """Each integration with its real state: only the openFDA import exists; ECRI needs a license; the rest are not built."""
    from apps.pm.models import PmProcedure

    out = []
    for key, code, name, description in INTEGRATIONS:
        status, detail = NOT_CONNECTED, ""
        if key == "fda":
            status, detail = _fda_status()
        elif key == "ecri":
            status, detail = LICENSE, "Needs a licensed ECRI feed"
        elif key == "oem":
            n = PmProcedure.objects.count()
            detail = f"{n} PM procedure{'' if n == 1 else 's'} entered in Cadence"
        out.append({"key": key, "code": code, "name": name, "description": description, "status": status, "detail": detail})
    return out


# --- risk scoring ----------------------------------------------------------------------------------------

RISK_RUBRIC = "Score = clinical function (1 to 10) + physical risk of failure (1 to 5) + maintenance requirement (1 to 5) + incident history (0 to 2)"
RISK_BANDS = [("16 and above", RiskClass.LIFE_SUPPORT), ("12 to 15", RiskClass.HIGH), ("9 to 11", RiskClass.MEDIUM), ("8 and below", RiskClass.LOW)]


def risk_summary() -> list[dict]:
    """The score bands with the active devices in each class (a device's class comes from its model)."""
    counts = dict(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).order_by().values_list("device_model__risk_class").annotate(n=Count("id")))
    return [{"band": band, "risk": rc.value, "label": rc.label, "devices": counts.get(rc.value, 0)} for band, rc in RISK_BANDS]


def risk_scoring_summary(today=None) -> dict:
    """How far the catalog's risk scoring has got (slice 14): its models, how many are scored, and how many scored ones are due
    their yearly review (apps.equipment.services.risk_review_due). The unscored models are the gap between the first two."""
    from apps.equipment.models import DeviceModel
    from apps.equipment.services import risk_review_due

    today = today or timezone.localdate()  # the facility's today, read once for every model
    models = list(DeviceModel.objects.only("risk_function", "risk_physical", "risk_maintenance", "risk_incidents", "risk_reviewed_on"))
    scored = [dm for dm in models if dm.risk_score is not None]
    return {"models": len(models), "scored": len(scored), "reviews_due": sum(1 for dm in scored if risk_review_due(dm, today))}


# --- the facility's time zone (slice 21) ------------------------------------------------------------------

# The US zones, first in the list and named as people say them; every other zone follows by its IANA name.
US_ZONES = [("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"), ("America/Phoenix", "Arizona"),
            ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"), ("Pacific/Honolulu", "Hawaii")]
_US_NAMES = dict(US_ZONES)
# The list offers the zones named for a place (and UTC). The older aliases this server also knows ("US/Eastern", "EST5EDT", and
# "Etc/GMT+5", which is five hours behind UTC, not ahead) are left off the list but still accepted, so one set through the API
# shows as it is.
_REGIONS = ("Africa/", "America/", "Antarctica/", "Arctic/", "Asia/", "Atlantic/", "Australia/", "Europe/", "Indian/", "Pacific/")
TIME_ZONE_EXAMPLE = "America/Chicago"


@cache
def zone_names() -> frozenset:
    """Every IANA zone this server knows (zoneinfo.available_timezones(): a walk of the zone files, so read once)."""
    return frozenset(available_timezones())


@cache
def _by_lower() -> dict:
    return {name.lower(): name for name in zone_names()}


def offset_text(name: str, at: datetime | None = None) -> str:
    """The zone's offset from UTC at `at` (now): "UTC-4", "UTC+5:30", "UTC". Daylight saving moves it, so it is worked out each time."""
    delta = (at or timezone.now()).astimezone(ZoneInfo(name)).utcoffset()
    minutes = int(delta.total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'+' if minutes > 0 else '-'}{hours}" + (f":{rest:02d}" if rest else "")


def zone_label(name: str) -> str:
    """A zone in words: "Eastern (America/New_York)" for the US zones, else its name, "Europe/London"."""
    us = _US_NAMES.get(name)
    return f"{us} ({name})" if us else name


def time_zone_choices(current: str = "", at: datetime | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """The Settings list, as (group, [(zone, label)]): the US zones, then every other zone named for a place, by name, each with
    its offset now. A current zone the list leaves out (an alias set through the API) heads the second group, so it shows as set."""
    at = at or timezone.now()
    us = [(name, f"{label} ({name}) · {offset_text(name, at)}") for name, label in US_ZONES]
    rest = sorted(name for name in zone_names() if name not in _US_NAMES and (name.startswith(_REGIONS) or name == "UTC"))
    if current and current in zone_names() and current not in _US_NAMES and current not in rest:
        rest.insert(0, current)
    others = [(name, f"{name.replace('_', ' ')} · {offset_text(name, at)}") for name in rest]
    return [("United States", us), ("Other time zones", others)]


def _zone_name(value) -> str:
    """`value` as the zone it names (in any letter case: "america/chicago" is America/Chicago), or ValidationError in plain words."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValidationError({"time_zone": f"Choose a time zone, like {TIME_ZONE_EXAMPLE}."})
    name = _by_lower().get(text.lower())
    if name is None:
        shown = f"{text} is" if len(text) <= 60 and text.isprintable() else "That is"
        raise ValidationError({"time_zone": f"{shown} not a time zone. Choose one from the list, like {TIME_ZONE_EXAMPLE}."})
    return name


HISTORY_BASELINE_REASON = "As on record when its changes began to be kept"


def _history_baseline(tenant: Tenant) -> None:
    """A facility added before its changes were kept (Tenant history arrived in slice 21) has no history yet: record it as it
    stands, dated when it was added, so its first recorded change reads with a before as well as an after."""
    history = Tenant.history.model
    if history.objects.filter(id=tenant.pk).exists():
        return
    values = {f.attname: getattr(tenant, f.attname) for f in history.tracked_fields}
    history.objects.create(**values, history_date=tenant.created_at, history_type="+", history_user=None,
                           history_change_reason=HISTORY_BASELINE_REASON)


def set_time_zone(tenant: Tenant, name, by=None) -> Tenant:
    """Set the facility's time zone (Tenant.timezone). From the next request on it is the facility's today (what is overdue, due
    dates, contract days left, report periods) and the clock its times are shown in; its daily jobs and emails follow it from its
    next day. Only a zone this server knows (zone_names(), in any letter case); anything else is refused in plain words, keyed
    "time_zone". Works only inside the facility it changes. Audited: the facility's history records who (`by`), before and after;
    setting the zone it already has records nothing. `tenant` is updated in place as well."""
    current = get_current_tenant()
    if tenant is None or current is None or current.pk != tenant.pk:
        raise RuntimeError("set_time_zone() works inside the facility it changes")
    zone = _zone_name(name)
    with transaction.atomic():
        row = Tenant.objects.select_for_update().get(pk=tenant.pk)
        if row.timezone != zone:
            _history_baseline(row)
            row.timezone = zone
            if by is not None:
                row._history_user = by
            row.save(update_fields=["timezone"])
    tenant.timezone = zone
    return row
