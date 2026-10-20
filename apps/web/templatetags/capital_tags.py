"""Slice 36, the replacement priority's screens: which links a reader may follow, and the device drawer's Expected life line.

- `opens_models`: the model drawer (web:pm_model) is the PM screen's: PM View, never a scoped user (web_view's default). The
  Replacement planning report links a model there only for a reader who may open it; a device link needs Equipment View (the views'
  can_view_asset).
- `expected_life`: the device drawer's Overview line, "10 yr, through FY2031" (apps.web.asset_tabs.expected_life).
"""
from django import template

from apps.pm import permissions as pm_perms
from apps.workorders import scoping

register = template.Library()


@register.filter
def opens_models(user) -> bool:
    return bool(user is not None and user.is_authenticated) and user.has_level(pm_perms.MODULE, pm_perms.VIEW_LEVEL) and not scoping.is_scoped(user)


@register.simple_tag
def expected_life(asset):
    """`{% expected_life asset as life %}`: life.words ("10 yr, through FY2031"), life.source ("from the manufacturer", or ""), and
    life.title (the day the life ends and the fiscal year's dates, or "")."""
    from ..asset_tabs import expected_life as line  # asset_tabs imports the web tags

    return line(asset)
