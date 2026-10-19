"""Slice 32: the Reports page's way into Program evaluation, shown to whoever may open it (apps.evaluations.permissions.can_view:
Evaluations View, never a scoped user). The pages check it again (web_view)."""
from django import template

from apps.evaluations import permissions as perms

register = template.Library()


@register.filter
def reads_evaluations(user) -> bool:
    return bool(user is not None and user.is_authenticated) and perms.can_view(user)


@register.filter
def reads_vendor_reviews(user) -> bool:
    """Slice 34: the Contracts screen's Reviews tab, for Evaluations View with Contracts View, never a scoped user."""
    return bool(user is not None and user.is_authenticated) and perms.can_view_reviews(user)
