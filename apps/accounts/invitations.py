"""
Invitation links: an invited account (created by services.invite_user with no usable password) gets an email with a
link that sets its first password and signs it in. The link is a signed token, not a stored secret:

- it names the account and is valid for settings.INVITATION_VALID_DAYS;
- it stops working once the password is set or the account signs in (both change the hashed state), when the account is
  deactivated, and when a newer invitation is sent (invited_at is part of the hash), so only the latest email works.

Views: web.views_invite (accept). Senders: the Users screen (invite, Resend invite), bootstrap_tenant --invite, and the
password-reset form, which sends a pending account a fresh invitation instead of a reset link.

Access events (slice 20): a send that replaces an earlier invitation's link (Resend invite, the API's resend-invite) writes an
"Invitation resent" AccessEvent once the email has gone out. The first send writes none: invite_user's "Invited" event is that one,
and a first email that failed and is sent later is still the first link. A failed send changes nothing, so it writes nothing. A
fresh invitation the signed-out password-reset form sends writes none either (slice 22): no one on the staff changed anything, and
an event there would tell the facility that someone asked for a reset of that address.

One person in several facilities (slice 22, apps.accounts.people): an invitation for a person who already signs in to Cadence CE
at another facility (a linked account whose person `can_sign_in`) never carries a set-password link. Its email says they were
added to the facility and links to the join page (/account/facility/<pk>/join/), where they join while signed in, with the
password they have. A set-password link for such an account is refused (pending_user_from_uid), so a link sent before they had a
password never replaces it. Otherwise the ordinary invitation: accepting it sets the person's password, which their other accounts
then share (User.save).
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

from . import emails, people
from .models import AccessEvent, Role, User
from .services import record_access_event


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


def joins_signed_in(user) -> bool:
    """Whether `user`'s invitation is joined while signed in rather than accepted by setting a password: a linked account whose
    person already signs in to another facility (slice 22). Reads User and Tenant only, so it is safe with no tenant set."""
    return bool(user.person) and people.can_sign_in(user)


def join_url(user) -> str:
    """The join page for `user`'s invitation: it opens once the person has signed in (to any of their facilities)."""
    return settings.APP_BASE_URL + reverse("web:facility_join", args=[user.pk])


def link_for(user) -> str:
    """The link `user`'s invitation email carries: the join page, or the set-password link (so compute it after send_invitation)."""
    return join_url(user) if joins_signed_in(user) else invitation_url(user)


def _role_name(user) -> str:
    """Read fresh inside the invitee's facility, never from user.role: Role is tenant-scoped, and a sender with no tenant in
    context (bootstrap_tenant, the signed-out password-reset form) would find a role loaded earlier hidden by row-level
    security, and a cached relation would keep that answer."""
    if not user.role_id or not user.tenant_id:
        return ""
    with tenant_context(user.tenant):
        return Role.objects.filter(pk=user.role_id).values_list("name", flat=True).first() or ""


def send_invitation(user, *, by=None, resend=False) -> bool:
    """Email `user` a fresh link to set their first password; any earlier link stops working. For a person who already signs in
    elsewhere (joins_signed_in) the email says they were added to the facility and links to the join page instead. False if the
    email could not be sent: then nothing changes (an earlier link still works, the account stays Invited, and the Users screen's
    Resend invite tries again). Raises ValidationError for an account that is not a pending invitation.

    `resend=True` is the staff's Resend invite (the Users screen, the API): once the email has gone out it writes "Invitation
    resent". Nothing else writes it: not the first send (invite_user's Invited event is that one), and not the signed-out
    password-reset form (slice 22 review: no staff change, and whether it wrote one must not depend on anything the reset form
    touched, such as invited_at, or the facility could tell an address used at another facility from a new one)."""
    if not is_pending(user):
        raise ValidationError(f"{user.get_full_name() or user.email} already has a password or is deactivated; there is no invitation to send.")
    if not user.email:
        raise ValidationError("This account has no email address.")
    previous = user.invited_at
    user.invited_at = timezone.now()
    user.save(update_fields=["invited_at"])  # saved first, so the emailed link always matches what is stored
    joins = joins_signed_in(user)
    context = {"user": user, "by": by, "facility": user.tenant.name if user.tenant_id else "Cadence CE", "role": _role_name(user),
               "url": join_url(user) if joins else invitation_url(user), "valid_days": settings.INVITATION_VALID_DAYS}
    if emails.send(user.email, "accounts/email/facility_added" if joins else "accounts/email/invitation", context):
        # The same event whichever email went out: the facility sees an invitation resent, never which kind.
        if resend and user.tenant_id:
            record_access_event(user.tenant, AccessEvent.Action.INVITATION_RESENT, by=by, user=user, role_id=user.role_id,
                                detail=f"A new link to {user.email} replaces the earlier one")
        return True
    # Nothing went out: put the earlier invitation back, so its link (if one was sent) keeps working.
    user.invited_at = previous
    user.save(update_fields=["invited_at"])
    return False


def pending_user_from_uid(uidb64: str):
    """The account a link's uid names when it is a pending invitation and its facility (if it has one) is active; else None.
    Never an account whose person already signs in elsewhere (slice 22): they join while signed in, so a set-password link sent
    before they had a password stops working (the page says to sign in as usual). Garbage in the URL gives None, never an error.
    The token is checked separately: user_for_link, or the accept view. Reads User and Tenant only (signed out, no tenant set)."""
    try:
        pk = int(urlsafe_base64_decode(uidb64).decode())
        user = User._default_manager.select_related("tenant").filter(pk=pk).first()
    except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
        return None
    if user is None or not is_pending(user) or (user.tenant_id and not user.tenant.is_active) or joins_signed_in(user):
        return None
    return user


def user_for_link(uidb64: str, token: str):
    """The pending account a link is for, or None when the link is malformed, expired, replaced, or already used."""
    user = pending_user_from_uid(uidb64)
    return user if user is not None and invitation_tokens.check_token(user, token) else None
