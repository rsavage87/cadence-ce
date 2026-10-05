"""
The account menu's Notifications link (slice 20, part C): shown to the people the page is for (apps.notifications.services.refusal:
in this facility, the whole facility's view, Work orders View). The view checks the same again on every request; this only keeps a
link that would answer 403 out of the menu.
"""
from django import template

from apps.notifications.services import can_choose

register = template.Library()


@register.filter
def chooses_notifications(user) -> bool:
    """{% if user|chooses_notifications %}: whether the Notifications page is offered to `user`."""
    return bool(getattr(user, "is_authenticated", False)) and can_choose(user)
