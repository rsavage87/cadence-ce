"""
Access levels for the program's annual evaluation (slice 32), shared by the web UI and the API so neither is the weaker door. A module of
its own (Module.EVALUATIONS): Reports Approve would let a read-only finance role sign off, and Reports Edit is the custom report
builder's. View reads; Edit drafts, refreshes the figures, writes the text and the objectives, sends it to committee, takes it back to
draft, and discards a draft; Approve records the committee's approvals, corrects or removes one, and amends an approved evaluation.
Scoped users (a vendor's company, a requester's unit) are refused everywhere. The figures also need Reports View and each indicator's
own areas (apps.evaluations.indicators: the cut), as the survey binder's sections do.
"""
from apps.accounts.models import Level, Module
from apps.workorders import scoping

MODULE = Module.EVALUATIONS
VIEW_LEVEL = Level.VIEW
EDIT_LEVEL = Level.EDIT
APPROVE_LEVEL = Level.APPROVE
FIGURES_MODULE = Module.REPORTS  # the figures, as the binder: Reports View, plus each indicator's needs


def can_view(user) -> bool:
    return user.has_level(MODULE, VIEW_LEVEL) and not scoping.is_scoped(user)


def can_edit(user) -> bool:
    return user.has_level(MODULE, EDIT_LEVEL) and not scoping.is_scoped(user)


def can_approve(user) -> bool:
    return user.has_level(MODULE, APPROVE_LEVEL) and not scoping.is_scoped(user)


def can_see_figures(user) -> bool:
    return can_view(user) and user.has_level(FIGURES_MODULE, Level.VIEW)
