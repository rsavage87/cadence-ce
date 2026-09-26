"""Chips and labels for the Users and access screen."""
from django import template
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
def level_label(level):
    try:
        return Level(int(level)).label
    except (TypeError, ValueError):
        return Level.NONE.label
