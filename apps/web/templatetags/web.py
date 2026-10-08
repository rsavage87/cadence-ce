"""Small renderers shared by the web templates: icons, status chips, PM stickers, money, and query strings.

Days count from the facility's today (timezone.localdate(): the tenant middleware activates its time zone), never the server's."""
from urllib.parse import urlencode

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.equipment.models import AssetStatus, RiskClass
from apps.equipment.services import AWAITING_LABEL, HELD_LABEL, status_label
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
    "mine": ("1.7", '<rect x="5" y="4" width="14" height="17" rx="2"/><circle cx="12" cy="10.5" r="2.5"/><path d="M8 18a4 4 0 0 1 8 0"/>'),
    "wo": ("1.7", '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 12l2 2 4-4"/>'),
    "search": ("1.8", '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    "plus": ("2", '<path d="M12 5v14M5 12h14"/>'),
    "x": ("2", '<path d="M6 6l12 12M18 6 6 18"/>'),
    "l": ("2", '<path d="m15 5-7 7 7 7"/>'),
    "r": ("2", '<path d="m9 5 7 7-7 7"/>'),
    "link": ("1.8", '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.5 1.5M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5"/>'),
    "contract": ("1.7", '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 12.5h6M10 16.5h6"/>'),
    "recall": ("1.7", '<path d="M12 3 2.5 20h19zM12 10v4M12 17.5v.5"/>'),
    "incident": ("1.7", '<path d="M5 21V4M5 4h12l-2.5 4.5L17 13H5"/>'),  # slice 28: a flag, the incident reported
    "sync": ("1.8", '<path d="M20 12a8 8 0 0 1-14.5 4.6M4 12a8 8 0 0 1 14.5-4.6M18 3v5h-5M6 21v-5h5"/>'),
    "rep": ("1.7", '<path d="M4 20h16M6 16V9M11 16V5M16 16v-6"/>'),
    "dl": ("1.8", '<path d="M12 4v11M7 10l5 5 5-5M4 20h16"/>'),
    "print": ("1.8", '<path d="M7 8V4h10v4M5 17H4a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-1M7 14h10v6H7z"/>'),
    "scan": ("1.8", '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M4 12h16"/>'),
    "pm": ("1.7", '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'),
    "set": ("1.7", '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'),
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


AWAITING_CSS = "info"  # slice 26: a device out of service waiting for its incoming inspection is on hold, not broken
HELD_CSS = "crit"  # slice 28: held as evidence: nobody touches it


@register.simple_tag
def asset_status_chip(device_or_status):
    """A device's status chip. Given the device (slice 26), it reads as every screen, CSV, and the API word it
    (equipment.services.status_label: "Awaiting inspection" for one out of service waiting for its incoming inspection); given a
    status alone, that status."""
    if isinstance(device_or_status, str):
        return _chip(ASSET_STATUS_CSS.get(device_or_status, "neutral"), AssetStatus(device_or_status).label)
    label = status_label(device_or_status)
    if label == HELD_LABEL:  # slice 28: held as evidence for an incident investigation
        return _chip(HELD_CSS, label)
    return _chip(AWAITING_CSS if label == AWAITING_LABEL else ASSET_STATUS_CSS.get(device_or_status.status, "neutral"), label)


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
    if asset.awaiting_inspection:  # slice 26: its PM schedule starts when its incoming inspection passes
        return format_html('<span class="stk insp" title="{}">{}</span>', "Its PMs start when its incoming inspection passes", "After inspection")
    if not asset.next_pm_on:
        return format_html('<span class="stk na">{}</span>', "Not scheduled")
    d = (asset.next_pm_on - timezone.localdate()).days
    if d < 0:
        return format_html('<span class="stk over">Overdue {} d</span>', -d)
    if d == 0:
        return format_html('<span class="stk due">{}</span>', "Due today")
    if d <= 30:
        return format_html('<span class="stk due">Due in {} d</span>', d)
    return format_html('<span class="stk ok">Due {}</span>', asset.next_pm_on.strftime("%b %Y"))


@register.simple_tag
def incoming_banner(asset, user):
    """The device drawer's incoming-inspection banner for `user` (slice 26; apps.web.asset_tabs.incoming_banner), or None for a device
    not waiting for its incoming inspection: `{% incoming_banner asset request.user as inc %}`. A tag, so every view that draws the
    drawer gets it."""
    from ..asset_tabs import incoming_banner as banner  # asset_tabs imports this module

    return banner(asset, user)


@register.simple_tag
def incident_box(asset, user):
    """The device drawer's hold banner and Record incident offer for `user` (slice 28; apps.web.asset_tabs.incident_box):
    `{% incident_box asset request.user as ib %}`, then ib.hold (None when not held) and ib.record."""
    from ..asset_tabs import incident_box as box  # asset_tabs imports this module

    return box(asset, user)


@register.simple_tag
def held_chip():
    """The "Held" chip (slice 28): a work order the device's hold keeps from starting or completing, a held device on the PM day list."""
    return format_html('<span class="chip {}" title="{}">{}</span>', HELD_CSS, "Held as evidence for an incident investigation", "Held")


@register.simple_tag
def held_words():
    """apps.incidents.services.HELD_WORDS (slice 28): a hold in words for anyone, a scoped user included (no incident number)."""
    from apps.incidents.services import HELD_WORDS

    return HELD_WORDS


@register.simple_tag
def local_now():
    """Now in the time zone at work (the facility's, in a request): `{% local_now as at %}{{ at|date:"g:i A T" }}`. Unlike Django's
    {% now %}, which reads the system clock itself, this reads django.utils.timezone.now like the rest of the app."""
    return timezone.localtime()


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
    return (d - timezone.localdate()).days if d else None


@register.filter
def days_since(d):
    return (timezone.localdate() - d).days if d else None


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
