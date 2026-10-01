"""
PM procedures in the model drawer (slice 14): the Procedure tab, choosing a model's procedure, and writing or revising one.
Views parse input, call apps.pm.procedures, and answer with the model drawer on the Procedure tab
(views_models.render_model_drawer).

TODO(slice 14, part D).
"""
from apps.pm import permissions as pm_perms

from .decorators import web_view


def procedure_tab(request, dm) -> dict:
    """The Procedure tab's context. TODO(part D)."""
    return {}


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_procedure(request, pk):
    raise NotImplementedError  # TODO(part D)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def procedure_new(request):
    raise NotImplementedError  # TODO(part D)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def procedure_edit(request, pk):
    raise NotImplementedError  # TODO(part D)
