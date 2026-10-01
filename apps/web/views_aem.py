"""
The AEM tab of the model drawer (slice 14): the model's AEM status and history, the evidence, and proposing, approving or
rejecting, withdrawing, and ending an AEM interval. Views parse input, call apps.pm.aem, and answer with the model drawer on the
AEM tab (views_models.render_model_drawer).

TODO(slice 14, part B).
"""
from apps.pm import permissions as pm_perms

from .decorators import web_view


def aem_tab(request, dm) -> dict:
    """The AEM tab's context. TODO(part B)."""
    return {}


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_propose(request, pk):
    raise NotImplementedError  # TODO(part B)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_decide(request, pk):
    raise NotImplementedError  # TODO(part B)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_withdraw(request, pk):
    raise NotImplementedError  # TODO(part B)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def aem_end(request, pk):
    raise NotImplementedError  # TODO(part B)
