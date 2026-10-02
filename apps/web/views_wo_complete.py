"""
Completing a work order from its drawer (slice 15): "Mark completed" opens a modal for the resolution and, on a PM, the checklist
results and the overall result. Views parse input, call apps.workorders.completion, and answer with the drawer.

TODO(slice 15, part B).
"""
from apps.workorders import permissions as wo_perms

from .decorators import web_view


def results_context(request, wo) -> dict:
    """The drawer's PM results section (_wo_results.html). TODO(part B)."""
    return {}


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def wo_complete(request, number):
    raise NotImplementedError  # TODO(part B)
