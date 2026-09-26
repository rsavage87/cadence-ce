"""Chips and labels for recalls, shared by the Recalls screen, the device drawer, and the Overview."""
from django import template
from django.utils.html import format_html

from apps.recalls.models import Alert, AlertMatch

register = template.Library()

# The mock's rcChip colors: needs action crit, under review warn, in progress info, closed ok, not affected neutral.
STATUS_CSS = {AlertMatch.Status.NEEDS_ACTION: "crit", AlertMatch.Status.UNDER_REVIEW: "warn", AlertMatch.Status.IN_PROGRESS: "info",
              AlertMatch.Status.CLOSED: "ok", AlertMatch.Status.NOT_AFFECTED: "neutral"}


@register.simple_tag
def alert_status_chip(status):
    return format_html('<span class="chip {}">{}</span>', STATUS_CSS.get(status, "neutral"), AlertMatch.Status(status).label)


@register.filter
def alert_label(alert: Alert) -> str:
    """"FDA Z-2026-4408": the source and the notice's own number."""
    return f"{alert.get_source_display()} {alert.external_id}"


@register.filter
def is_class_one(alert: Alert) -> bool:
    return "class i" == alert.classification.strip().lower() or alert.classification.strip().lower().startswith("class i ")
