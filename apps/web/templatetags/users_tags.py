"""Chips and labels for the Users and access screen."""
from datetime import timedelta

from django import template
from django.utils import timezone
from django.utils.html import format_html

from apps.accounts.models import Level
from apps.accounts.services import USER_STATUSES

register = template.Library()

USER_STATUS_CSS = {"active": "ok", "invited": "info", "deactivated": "neutral"}
USER_STATUS_LABEL = dict(USER_STATUSES)


@register.simple_tag
def user_status_chip(status):
    return format_html('<span class="chip {}">{}</span>', USER_STATUS_CSS.get(status, "neutral"), USER_STATUS_LABEL.get(status, status))


@register.filter
def last_active(when):
    """The mock's "Last active" wording: "Today, 8:12 AM", "Yesterday", "Sep 19", or "Jun 2, 2026" for earlier years; "—" when never."""
    if not when:
        return "—"
    local = timezone.localtime(when)
    today = timezone.localdate()
    if local.date() == today:
        return f"Today, {local.strftime('%-I:%M %p')}"
    if local.date() == today - timedelta(days=1):
        return "Yesterday"
    return local.strftime("%b %-d") if local.year == today.year else local.strftime("%b %-d, %Y")


@register.filter
def level_label(level):
    try:
        return Level(int(level)).label
    except (TypeError, ValueError):
        return Level.NONE.label
