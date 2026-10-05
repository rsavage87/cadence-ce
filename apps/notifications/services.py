"""
Notifications to staff (slice 20): who wants which emails. Part C adds setting preferences and the assignment emails here; the daily
digest and contract reminders (part D) live in apps/notifications/daily.py and only read preferences through preferences_for.
"""
from .models import NotificationPreference

KINDS = ("assignments", "daily_digest", "contract_reminders")


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
