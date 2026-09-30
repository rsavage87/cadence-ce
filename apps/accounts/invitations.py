"""
Invitation links: an invited account (created by services.invite_user with no usable password) gets an email with a
link that sets its first password and signs it in. The link is a signed token, not a stored secret:

- it names the account and is valid for settings.INVITATION_VALID_DAYS;
- it stops working once the password is set or the account signs in (both change the hashed state), when the account is
  deactivated, and when a newer invitation is sent (invited_at is part of the hash), so only the latest email works.

Views: web.views_invite (accept). Senders: the Users screen (invite, Resend invite), bootstrap_tenant --invite, and the
password-reset form, which sends a pending account a fresh invitation instead of a reset link.
"""
from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.encoding import force_bytes
from django.utils.http import base36_to_int, urlsafe_base64_decode, urlsafe_base64_encode

from apps.tenants.context import tenant_context

from . import emails
from .models import Role, User


def is_pending(user) -> bool:
    """Invited, still active, and never signed in: the only accounts an invitation link can set a password for."""
    return bool(user.is_invited and user.is_active and user.last_login is None and not user.has_usable_password())


class InvitationTokenGenerator(PasswordResetTokenGenerator):
    key_salt = "apps.accounts.invitations.InvitationTokenGenerator"

    def _make_hash_value(self, user, timestamp):
        # invited_at to the microsecond (timestamp() is the same whatever time zone the value was read in): two sends within
        # one second must still give different links, or a resend would not replace the earlier one.
        sent = "" if user.invited_at is None else repr(user.invited_at.timestamp())
        return f"{user.pk}{user.password}{user.last_login}{user.is_active}{sent}{user.email}{timestamp}"

    def check_token(self, user, token):
        """Django's check with the invitation's own lifetime instead of PASSWORD_RESET_TIMEOUT (which is for reset links)."""
        if not (user and token):
            return False
        try:
            ts_b36, _hash = token.split("-")
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False
        if not any(constant_time_compare(self._make_token_with_timestamp(user, ts, secret), token)
                   for secret in [self.secret, *self.secret_fallbacks]):
            return False
        return (self._num_seconds(self._now()) - ts) <= settings.INVITATION_VALID_DAYS * 24 * 60 * 60


invitation_tokens = InvitationTokenGenerator()


def invitation_url(user) -> str:
    """The link for `user`'s current invitation. Sending a new one changes it, so compute it after send_invitation."""
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    return settings.APP_BASE_URL + reverse("web:invite_accept", args=[uidb64, invitation_tokens.make_token(user)])


def _role_name(user) -> str:
    """Read fresh inside the invitee's facility, never from user.role: Role is tenant-scoped, and a sender with no tenant in
    context (bootstrap_tenant, the signed-out password-reset form) would find a role loaded earlier hidden by row-level
    security, and a cached relation would keep that answer."""
    if not user.role_id or not user.tenant_id:
        return ""
    with tenant_context(user.tenant):
        return Role.objects.filter(pk=user.role_id).values_list("name", flat=True).first() or ""


def send_invitation(user, *, by=None) -> bool:
    """Email `user` a fresh link to set their first password; any earlier link stops working. False if the email could not
    be sent: then nothing changes (an earlier link still works, the account stays Invited, and the Users screen's Resend
    invite tries again). Raises ValidationError for an account that is not a pending invitation."""
    if not is_pending(user):
        raise ValidationError(f"{user.get_full_name() or user.email} already has a password or is deactivated; there is no invitation to send.")
    if not user.email:
        raise ValidationError("This account has no email address.")
    previous = user.invited_at
    user.invited_at = timezone.now()
    user.save(update_fields=["invited_at"])  # saved first, so the emailed link always matches what is stored
    context = {"user": user, "by": by, "facility": user.tenant.name if user.tenant_id else "Cadence CE", "role": _role_name(user),
               "url": invitation_url(user), "valid_days": settings.INVITATION_VALID_DAYS}
    if emails.send(user.email, "accounts/email/invitation", context):
        return True
    # Nothing went out: put the earlier invitation back, so its link (if one was sent) keeps working.
    user.invited_at = previous
    user.save(update_fields=["invited_at"])
    return False


def pending_user_from_uid(uidb64: str):
    """The account a link's uid names when it is a pending invitation and its facility (if it has one) is active; else None.
    Garbage in the URL gives None, never an error. The token is checked separately: user_for_link, or the accept view."""
    try:
        pk = int(urlsafe_base64_decode(uidb64).decode())
        user = User._default_manager.select_related("tenant").filter(pk=pk).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None
    if user is None or not is_pending(user) or (user.tenant_id and not user.tenant.is_active):
        return None
    return user


def user_for_link(uidb64: str, token: str):
    """The pending account a link is for, or None when the link is malformed, expired, replaced, or already used."""
    user = pending_user_from_uid(uidb64)
    return user if user is not None and invitation_tokens.check_token(user, token) else None
