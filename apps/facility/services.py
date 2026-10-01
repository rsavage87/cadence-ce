"""
Facility settings (slice 8): the service request portal, maintenance policy text, KPI targets, the integration list,
and the risk-scoring summary the Settings screen shows. Views and the API call these; nothing sets fields directly.

Reads never write: get_settings() returns unsaved defaults until someone saves. Every save records who made it
(django-simple-history), because a change to policy or targets changes what surveyors and managers are shown.

Portal emails (slice 13): with the confirmation set to "On screen and by email", the portal may email a requester, but only at one
of the facility's own work email domains, so the public form can never be used to send mail anywhere else. at_domains() is the
one check: the portal form refuses another domain, and apps.portal.notifications asks email_allowed() again at sending time,
because the setting or the domains may have changed since the request came in.
"""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.urls import reverse
from django.utils import timezone

from apps.equipment.models import Asset, RiskClass
from apps.tenants.context import get_current_tenant

from .models import POLICY, POLICY_DEFAULTS, POLICY_MAX_LENGTH, FacilitySettings

PORTAL_FIELDS = ("portal_require_callback", "portal_hotline", "portal_confirmation", "portal_email_domains")
POLICY_FIELDS = tuple(field for field, _label, _default in POLICY)
TARGET_FIELDS = ("target_pm_pct", "target_uptime_pct", "target_mttr_days", "repair_budget_monthly")
EDITABLE = PORTAL_FIELDS + POLICY_FIELDS + TARGET_FIELDS

# (field, label, low, high): the ranges a target may take. Life support and high risk PM stay at 100% (survey rule).
TARGET_RANGES = [("target_pm_pct", "PM completion target", Decimal("50"), Decimal("100")),
                 ("target_uptime_pct", "Fleet uptime target", Decimal("90"), Decimal("100")),
                 ("target_mttr_days", "Mean time to repair target", Decimal("0.5"), Decimal("30"))]
LIFE_SUPPORT_PM_TARGET = 100.0
HOTLINE_MAX_LENGTH = 40
BUDGET_MAX = Decimal("9999999999.99")  # the largest value the DecimalField(12, 2) column holds

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
    exponent = d.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places:
        errors[field] = f"Use at most {places} decimal place{'' if places == 1 else 's'}."
        return None
    return d + 0  # drops a negative zero's sign


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
        if field == "portal_require_callback":
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
        for field, value in cleaned.items():
            setattr(s, field, value)
        if by is not None:
            s._history_user = by
        s.save()
    return s


def reset_policy(by=None) -> FacilitySettings:
    return update_settings(by=by, **POLICY_DEFAULTS)


def policy_items(s: FacilitySettings | None = None) -> list[dict]:
    """The eight policy lines for the screen: field, label, current text, default text."""
    s = s or get_settings()
    return [{"field": field, "label": label, "text": getattr(s, field), "default": default, "is_default": getattr(s, field) == default}
            for field, label, default in POLICY]


def kpi_targets(s: FacilitySettings | None = None) -> dict:
    """What the Overview tiles and the PM trend line compare against, as floats."""
    s = s or get_settings()
    return {"pm_on_time": float(s.target_pm_pct), "pm_on_time_life_support": LIFE_SUPPORT_PM_TARGET, "uptime_pct": float(s.target_uptime_pct),
            "mttr_days": float(s.target_mttr_days),
            "repair_budget_monthly": float(s.repair_budget_monthly) if s.repair_budget_monthly is not None else None}


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

    models = list(DeviceModel.objects.only("risk_function", "risk_physical", "risk_maintenance", "risk_incidents", "risk_reviewed_on"))
    scored = [dm for dm in models if dm.risk_score is not None]
    return {"models": len(models), "scored": len(scored), "reviews_due": sum(1 for dm in scored if risk_review_due(dm, today))}
