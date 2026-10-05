"""
Notifications to staff (slice 20): who wants which emails, and choosing them. The daily digest and contract reminders (part D) live in
apps/notifications/daily.py and only read preferences through preferences_for and wants; the assignment emails are
apps/notifications/assignments.py.

Choosing is self-service, on the account menu's Notifications page and at /api/v1/notification-preferences/: a user only ever sets their
own, and the emails always go to their own account's address. The page is for people who see the whole facility and can view work
orders (`refusal`): a scoped role (apps.workorders.scoping: a vendor's company, a requester's unit) is never offered it, since every one
of these emails is about the facility's work. Contract reminders are offered only to someone with Contracts Edit, who can act on them
(`offered`); without it they are never sent, whatever was saved, and cannot be turned on.

Until someone first saves, preferences_for gives the model's defaults (assignments on, digest off, contract reminders on); the first
save creates their row. Turning an email off always works.
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Level, Module
from apps.tenants.context import get_current_tenant
from apps.workorders.scoping import is_scoped

from .models import NotificationPreference

KINDS = ("assignments", "daily_digest", "contract_reminders")
# Each kind as the Notifications page and the toast name it, and what it sends.
LABELS = {
    "assignments": "Work orders assigned to me",
    "daily_digest": "Daily digest",
    "contract_reminders": "Contract reminders",
}
SHORT = {"assignments": "Assignment emails", "daily_digest": "Daily digest", "contract_reminders": "Contract reminders"}
CONTRACTS_LEVEL = Level.EDIT  # contract reminders ask the reader to renew or let a contract end: Contracts Edit acts on that
NOT_OFFERED = "Contract reminders go to people who can edit contracts (Contracts Edit)."


def preferences_for(user) -> NotificationPreference:
    """The user's saved preferences in the current facility, or the defaults (unsaved) until they first save them."""
    found = NotificationPreference.objects.filter(user=user).first()
    return found if found is not None else NotificationPreference(user=user)


def wants(user, kind: str) -> bool:
    """Whether `user` gets the emails of `kind` (a field of NotificationPreference: assignments, daily_digest, contract_reminders):
    their choice, and only while they can receive email at all (active, an address)."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not user.is_active or not user.email:
        return False
    return bool(getattr(preferences_for(user), kind))


# --- who may choose -----------------------------------------------------------------------------------------------------------

def refusal(user) -> str | None:
    """Why `user` cannot choose emails in the current facility, in plain words; None when they can. The web view and the API check
    the levels and the scope first (web_view, ModulePermission); this says it again for the service and for the account menu."""
    tenant = get_current_tenant()
    if not getattr(user, "is_authenticated", False) or tenant is None or user.tenant_id != tenant.id:
        return "Only people in this facility choose the emails it sends them."
    if is_scoped(user):
        return "Your role sees only part of the facility, and these emails are about the whole facility's work."
    if not user.has_level(Module.WORKORDERS, Level.VIEW):
        return "You need Work orders View to get emails about work orders."
    return None


def can_choose(user) -> bool:
    """Whether the account menu offers `user` the Notifications page."""
    return refusal(user) is None


def offered(user, kind: str) -> bool:
    """Whether `user` is offered the emails of `kind`: contract reminders need Contracts Edit; the others, everyone who may choose."""
    if kind not in KINDS:
        raise ValueError(kind)
    return kind != "contract_reminders" or user.has_level(Module.CONTRACTS, CONTRACTS_LEVEL)


def shown(user, prefs: NotificationPreference | None = None) -> dict:
    """What `user` gets, kind by kind, as the page's switches and the API show it: their choice, and contract reminders off for someone
    who is not offered them, whatever was saved."""
    prefs = prefs or preferences_for(user)
    return {kind: bool(getattr(prefs, kind)) and offered(user, kind) for kind in KINDS}


# --- choosing -----------------------------------------------------------------------------------------------------------------

def set_preferences(user, **choices) -> NotificationPreference:
    """Save `user`'s own choices: any of KINDS, each True or False; the kinds left out keep what they were. Creates their row on the
    first save. Raises ValidationError for an unknown kind, a value that is not True or False, a user who may not choose (refusal),
    and turning on contract reminders without Contracts Edit. Turning them off without it is accepted and changes nothing: they are
    already off for that user, and what was saved applies again if they are given Contracts Edit."""
    unknown = sorted(set(choices) - set(KINDS))
    if unknown:
        raise ValidationError(f"Unknown notification: {', '.join(unknown)}.")
    errors = {kind: "Choose on or off." for kind, value in choices.items() if not isinstance(value, bool)}
    if errors:
        raise ValidationError(errors)
    why = refusal(user)
    if why:
        raise ValidationError(why)
    if not offered(user, "contract_reminders"):
        if choices.get("contract_reminders"):
            raise ValidationError({"contract_reminders": NOT_OFFERED})
        choices.pop("contract_reminders", None)
    prefs = preferences_for(user)
    if prefs._state.adding:  # the first save: the row starts with these choices and the defaults for the rest
        for kind, value in choices.items():
            setattr(prefs, kind, value)
        try:
            with transaction.atomic():  # a savepoint: another tab may create the row at the same moment
                prefs.save()
            return prefs
        except IntegrityError:
            prefs = preferences_for(user)  # it did: change that row instead
    changed = [kind for kind, value in choices.items() if getattr(prefs, kind) != value]
    if not changed:
        return prefs  # nothing changes: no save
    for kind in changed:
        setattr(prefs, kind, choices[kind])
    prefs.save(update_fields=[*changed, "updated_at"])
    return prefs
