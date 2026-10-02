"""
Labor and parts in the work order drawer (slice 15). Views parse input, call apps.workorders.costs, and answer with the drawer.

TODO(slice 15, part A).
"""
from django.views.decorators.http import require_POST

from apps.workorders import permissions as wo_perms

from .decorators import web_view


def costs_context(request, wo) -> dict:
    """The drawer's Cost section (_wo_costs.html). TODO(part A)."""
    return {}


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def labor_add(request, number):
    raise NotImplementedError  # TODO(part A)


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def labor_delete(request, number, pk):
    raise NotImplementedError  # TODO(part A)


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def part_add(request, number):
    raise NotImplementedError  # TODO(part A)


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def part_delete(request, number, pk):
    raise NotImplementedError  # TODO(part A)
