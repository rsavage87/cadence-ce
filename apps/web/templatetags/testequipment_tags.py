"""Slice 35: the Equipment screen's Test equipment tab, shown to whoever may open it (apps.testequipment.permissions.can_view: Equipment
View, never a scoped user). The pages check it again (web_view and the views)."""
from django import template

from apps.testequipment import permissions as perms

register = template.Library()


@register.filter
def reads_test_equipment(user) -> bool:
    return bool(user is not None and user.is_authenticated) and perms.can_view(user)
