"""Slice 32: the Reports page's way into Program evaluation, shown to whoever may open it (apps.evaluations.permissions.can_view:
Program evaluation View, never a scoped user). The pages check it again (web_view)."""
from django import template

from apps.evaluations import permissions as perms

register = template.Library()


@register.filter
def reads_evaluations(user) -> bool:
    return bool(user is not None and user.is_authenticated) and perms.can_view(user)
