"""Small renderers shared by the web templates: icons, status chips, PM stickers, money, and query strings."""
from datetime import date
from urllib.parse import urlencode

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.equipment.models import AssetStatus, RiskClass
from apps.workorders.models import Priority, WoStatus, WoType

register = template.Library()

_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '{body}</svg>')
ICONS = {
    "brand": ("2.2", '<path d="M3 12h4l2-6 3 12 3-8 2 2h4"/>'),
    "facility": ("1.7", '<path d="M3 21h18M5 21V5a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v16M9 8h2M13 8h2M9 12h2M13 12h2M9 16h2M13 16h2"/>'),
    "dash": ("1.7", '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="5" rx="1.5"/>'
                    '<rect x="13" y="12" width="8" height="9" rx="1.5"/><rect x="3" y="15" width="8" height="6" rx="1.5"/>'),
    "eq": ("1.7", '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4M6 11h3l1.5-3 2 6 1.5-3h4"/>'),
    "wo": ("1.7", '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 12l2 2 4-4"/>'),
    "search": ("1.8", '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    "plus": ("2", '<path d="M12 5v14M5 12h14"/>'),
    "x": ("2", '<path d="M6 6l12 12M18 6 6 18"/>'),
    "l": ("2", '<path d="m15 5-7 7 7 7"/>'),
    "r": ("2", '<path d="m9 5 7 7-7 7"/>'),
    "link": ("1.8", '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.5 1.5M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5"/>'),
    "contract": ("1.7", '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 12.5h6M10 16.5h6"/>'),
    "recall": ("1.7", '<path d="M12 3 2.5 20h19zM12 10v4M12 17.5v.5"/>'),
    "sync": ("1.8", '<path d="M20 12a8 8 0 0 1-14.5 4.6M4 12a8 8 0 0 1 14.5-4.6M18 3v5h-5M6 21v-5h5"/>'),
    "users": ("1.7", '<circle cx="9" cy="8" r="3.5"/><path d="M3 20a6 6 0 0 1 12 0M17 6.5a2.5 2.5 0 1 1 0 5M15.5 14.5A5 5 0 0 1 21 19.5"/>'),
    "moon": ("1.8", '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
    "sun": ("1.8", '<circle cx="12" cy="12" r="4"/>'
                   '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
}


@register.simple_tag
def icon(name):
    w, body = ICONS[name]
    return mark_safe(_SVG.format(w=w, body=body))  # static markup from ICONS above, no user input


def _chip(css, label):
    return format_html('<span class="chip {}">{}</span>', css, label)


RISK_CSS = {RiskClass.LIFE_SUPPORT: "crit", RiskClass.HIGH: "warn", RiskClass.MEDIUM: "info", RiskClass.LOW: "neutral"}
ASSET_STATUS_CSS = {AssetStatus.IN_SERVICE: "ok", AssetStatus.IN_REPAIR: "violet", AssetStatus.OUT_OF_SERVICE: "crit", AssetStatus.ON_LOAN: "acc",
                    AssetStatus.MISSING: "warn", AssetStatus.RETIRED: "neutral"}
WO_STATUS_CSS = {WoStatus.OPEN: "neutral", WoStatus.IN_PROGRESS: "info", WoStatus.AWAITING_PARTS: "warn", WoStatus.COMPLETED: "ok"}
PRIORITY_CSS = {Priority.CRITICAL: "crit", Priority.HIGH: "warn"}
WO_TYPE_SHORT = {WoType.PM: "PM", WoType.REPAIR: "Repair", WoType.INSPECTION: "Inspection", WoType.RECALL: "Recall", WoType.SAFETY: "Safety"}


@register.simple_tag
def risk_chip(risk):
    return _chip(RISK_CSS.get(risk, "neutral"), RiskClass(risk).label)


@register.simple_tag
def asset_status_chip(status):
    return _chip(ASSET_STATUS_CSS.get(status, "neutral"), AssetStatus(status).label)


@register.simple_tag
def wo_status_chip(status):
    return _chip(WO_STATUS_CSS.get(status, "neutral"), WoStatus(status).label)


@register.simple_tag
def priority_chip(priority):
    return _chip(PRIORITY_CSS.get(priority, "neutral"), Priority(priority).label)


@register.filter
def wo_type_short(t):
    return WO_TYPE_SHORT.get(t, t)


@register.simple_tag
def pm_sticker(asset):
    if asset.status == AssetStatus.RETIRED:
        return format_html('<span class="stk na">{}</span>', "Retired")
    if not asset.next_pm_on:
        return format_html('<span class="stk na">{}</span>', "Not scheduled")
    d = (asset.next_pm_on - date.today()).days
    if d < 0:
        return format_html('<span class="stk over">Overdue {} d</span>', -d)
    if d == 0:
        return format_html('<span class="stk due">{}</span>', "Due today")
    if d <= 30:
        return format_html('<span class="stk due">Due in {} d</span>', d)
    return format_html('<span class="stk ok">Due {}</span>', asset.next_pm_on.strftime("%b %Y"))


@register.filter
def money(v):
    return f"${float(v or 0):,.0f}"


def money_k(v) -> str:
    v = float(v or 0)
    if abs(v) >= 1e6:
        return f"${v / 1e6:.2f}M"
    if abs(v) >= 1e4:
        return f"${v / 1e3:.1f}k"
    return f"${v:,.0f}"


register.filter("money_k", money_k)


@register.filter
def short_name(name):
    parts = (name or "").split()
    return f"{parts[0]} {parts[1][0]}." if len(parts) > 1 else (name or "")


@register.filter
def days_until(d):
    return (d - date.today()).days if d else None


@register.filter
def days_since(d):
    return (date.today() - d).days if d else None


@register.simple_tag
def query(params, **changes):
    """`?`-prefixed query string: `params` (a QueryDict) with `changes` applied. A change of None removes the key."""
    data = {k: params.getlist(k) for k in params}
    for k, v in changes.items():
        if v is None or v == "":
            data.pop(k, None)
        else:
            data[k] = [v]
    return "?" + urlencode(data, doseq=True) if data else ""
