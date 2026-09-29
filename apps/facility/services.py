"""
Facility settings (slice 8): the service request portal, maintenance policy text, KPI targets, the integration list,
and the risk-scoring summary the Settings screen shows. Views and the API call these; nothing sets fields directly.

Reads never write: get_settings() returns unsaved defaults until someone saves. Every save records who made it
(django-simple-history), because a change to policy or targets changes what surveyors and managers are shown.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.urls import reverse

from apps.equipment.models import Asset, RiskClass

from .models import POLICY, POLICY_DEFAULTS, POLICY_MAX_LENGTH, FacilitySettings

PORTAL_FIELDS = ("portal_require_callback", "portal_hotline")
POLICY_FIELDS = tuple(field for field, _label, _default in POLICY)
TARGET_FIELDS = ("target_pm_pct", "target_uptime_pct", "target_mttr_days", "repair_budget_monthly")
EDITABLE = PORTAL_FIELDS + POLICY_FIELDS + TARGET_FIELDS

# (field, label, low, high): the ranges a target may take. Life support and high risk PM stay at 100% (survey rule).
TARGET_RANGES = [("target_pm_pct", "PM completion target", Decimal("50"), Decimal("100")),
                 ("target_uptime_pct", "Fleet uptime target", Decimal("90"), Decimal("100")),
                 ("target_mttr_days", "Mean time to repair target", Decimal("0.5"), Decimal("30"))]
LIFE_SUPPORT_PM_TARGET = 100.0
HOTLINE_MAX_LENGTH = 40


def get_settings() -> FacilitySettings:
    """The tenant's settings, or unsaved defaults when it has never saved any. Requires a tenant context."""
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
    return d


def _clean(s: FacilitySettings, fields: dict) -> dict:
    """Validate and normalize `fields` against `s`; returns the cleaned values or raises ValidationError (field -> message)."""
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
            text = " ".join(str(value or "").split())  # one line, no stray whitespace
            if len(text) > HOTLINE_MAX_LENGTH:
                errors[field] = f"Keep the hotline to {HOTLINE_MAX_LENGTH} characters, e.g. ext. 4400."
            cleaned[field] = text
        elif field in POLICY_FIELDS:
            text = " ".join(str(value or "").split())
            if not text:
                errors[field] = "Policy text cannot be empty. Use Reset to defaults to restore it."
            elif len(text) > POLICY_MAX_LENGTH:
                errors[field] = f"Keep each policy to {POLICY_MAX_LENGTH} characters."
            cleaned[field] = text
        elif field == "repair_budget_monthly":
            d = _decimal(value, field, errors)
            if d is not None and d < 0:
                errors[field] = "The budget cannot be negative."
            elif d is not None and d >= Decimal("1e10"):
                errors[field] = "That budget is too large."
            cleaned[field] = d.quantize(Decimal("0.01")) if d is not None and field not in errors else None
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


def update_settings(by=None, **fields) -> FacilitySettings:
    """Change any of EDITABLE. All or nothing: one invalid field rejects the whole change."""
    s = get_settings()
    for field, value in _clean(s, fields).items():
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


def portal_url(tenant, department: str | None = None) -> str:
    """The public request form for this tenant; with a department, the link pre-fills it."""
    url = f"{django_settings.PORTAL_BASE_URL.rstrip('/')}{reverse('portal:request', args=[tenant.slug])}"
    return f"{url}?{urlencode({'dept': department})}" if department else url


# --- integrations --------------------------------------------------------------------------------------

CONNECTED, NOT_CONNECTED, LICENSE = "connected", "not_connected", "license"
INTEGRATIONS = [
    # key, short code, name, what it does (the mock's list; only the FDA feed is built)
    ("fda", "FDA", "FDA recalls", "Recall notices for device models in the inventory, imported nightly and matched automatically"),
    ("ecri", "ECRI", "ECRI Alerts Tracker", "Hazard reports and recalls matched to the inventory"),
    ("oem", "OEM", "OEM PM library", "Standard PM procedures and intervals by manufacturer and model, with revision tracking"),
    ("ehr", "EHR", "EHR device association", "FHIR Device resources so patient-connected equipment appears in the chart and location"),
    ("rtls", "RTLS", "Real-time location", "Last-seen location for tagged mobile equipment, replacing manual room fields"),
    ("erp", "ERP", "Purchasing and ERP", "Parts purchase orders, receiving for incoming inspection, and capital requests"),
    ("sso", "SSO", "Single sign-on", "SAML login with role mapping from the hospital directory"),
    ("msg", "MSG", "Notifications", "Email and text for critical work orders, recall matches, and overdue life-support PMs"),
]


def integrations() -> list[dict]:
    """Each integration with its real state: only the openFDA feed exists; ECRI needs a license; the rest are not built."""
    from apps.pm.models import PmProcedure
    from apps.recalls.models import Alert, AlertMatch
    from apps.recalls.services import feed_imported_at

    out = []
    for key, code, name, description in INTEGRATIONS:
        status, detail = NOT_CONNECTED, ""
        if key == "fda":
            at = feed_imported_at()
            if at:
                matched = AlertMatch.objects.filter(alert__source=Alert.Source.FDA).count()
                status, detail = CONNECTED, f"Newest notice imported {at:%b} {at.day}, {at.year} · {matched} matched to this inventory"
            else:
                detail = "No notices imported yet"
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
