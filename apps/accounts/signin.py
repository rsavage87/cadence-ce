"""
Sign-in lockouts and password-reset requests (slice 10).

Lockouts count failed sign-ins in the Django cache under two keys: the login text as typed (lowercased and stripped) and
the client address. The login key is never an account: a lockout for a name nobody uses looks exactly like one for a real
account, so neither the message nor the timing says whether the account exists. Each key has a fixed window of
SIGNIN_WINDOW_MINUTES that starts at its first failure; reaching SIGNIN_MAX_FAILURES (or SIGNIN_MAX_FAILURES_PER_IP for
the address) refuses every sign-in on that key, correct password or not, until the window ends. The backend enforces it
(apps.accounts.backends), so every password check (the sign-in page and Admin's) is covered; the sign-in form checks
first only to say why.

Password reset: request_password_reset never tells its caller anything. Requests beyond the per-email-address and
per-caller limits are dropped silently, and an unknown, deactivated, or closed-facility address is answered exactly like
a real one: the page always says "if an account uses that address, a link is on its way".
"""
import hashlib
import math
import time

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from . import emails, invitations
from .models import User

HOUR = 60 * 60


def _norm(login) -> str:
    return (login or "").strip().lower()


def _key(kind: str, value: str) -> str:
    """Hashed, so any login text makes a valid cache key and the cache never holds typed addresses."""
    return f"signin:{kind}:{hashlib.sha256(value.encode()).hexdigest()}"


def _count(key: str, seconds: int) -> int:
    """Add one to the counter at `key`, starting a fixed window of `seconds` if there is none; returns the new count.
    add-then-incr, so concurrent failures are all counted (a read-modify-write would lose some)."""
    if cache.add(key, 0, seconds):
        cache.set(f"{key}:until", time.time() + seconds, seconds)
    try:
        return cache.incr(key)
    except ValueError:  # the window ended between add and incr: this failure starts the next one
        cache.set(key, 1, seconds)
        cache.set(f"{key}:until", time.time() + seconds, seconds)
        return 1


def _over(key: str, limit: int) -> bool:
    return (cache.get(key) or 0) >= limit


def _duration(seconds: float) -> str:
    """'2 hours', '15 minutes', '1 minute': rounded up, never below a minute."""
    minutes = max(1, math.ceil(seconds / 60))
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _time_left(key: str, seconds: int) -> str:
    until = cache.get(f"{key}:until")
    return _duration(seconds if until is None else until - time.time())


def _window() -> int:
    return settings.SIGNIN_WINDOW_MINUTES * 60


# --- sign-in lockouts ------------------------------------------------------------------------------

def is_locked(login, ip) -> str | None:
    """Why sign-in is refused for this login text from this address, or None. Says nothing about whether an account exists."""
    login = _norm(login)
    if login:
        key = _key("login", login)
        if _over(key, settings.SIGNIN_MAX_FAILURES):
            return f"Too many failed sign-ins. Try again in {_time_left(key, _window())} or reset your password."
    if ip:
        key = _key("ip", str(ip))
        if _over(key, settings.SIGNIN_MAX_FAILURES_PER_IP):
            return f"Too many failed sign-ins from this network. Try again in {_time_left(key, _window())}."
    return None


def record_failure(login, ip) -> None:
    login = _norm(login)
    if login:
        _count(_key("login", login), _window())
    if ip:
        _count(_key("ip", str(ip)), _window())


def clear_failures(login, user=None) -> None:
    """Forget the failures for `login`, and for `user`'s username and email when given (a successful sign-in or a reset
    proves who they are, whichever name they were typed under). The address counter is left alone: one good sign-in from
    a shared network does not excuse the failures from it."""
    names = {_norm(login)}
    if user is not None:
        names |= {_norm(user.username), _norm(user.email)}
    keys = [_key("login", n) for n in names if n]
    cache.delete_many([*keys, *(f"{k}:until" for k in keys)])


# --- password reset --------------------------------------------------------------------------------

def reset_link_lifetime() -> str:
    """How long a reset link works, in words, from PASSWORD_RESET_TIMEOUT ('2 hours')."""
    return _duration(settings.PASSWORD_RESET_TIMEOUT)


def password_reset_url(user) -> str:
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    return settings.APP_BASE_URL + reverse("web:password_reset_confirm", args=[uidb64, default_token_generator.make_token(user)])


def send_password_reset(user) -> bool:
    """Email `user` a link to set a new password. It works for PASSWORD_RESET_TIMEOUT and only once (the token hashes the
    password and the last sign-in). False if the email could not be sent."""
    context = {"user": user, "url": password_reset_url(user), "valid_for": reset_link_lifetime(),
               "facility": user.tenant.name if user.tenant_id else ""}
    return emails.send(user.email, "accounts/email/password_reset", context)


def can_reset(user) -> bool:
    """Active, and its facility (if it has one) is active: the only accounts a reset email or link is for."""
    return bool(user.is_active and (user.tenant_id is None or user.tenant.is_active))


def _allowed(kind: str, value: str, limit: int) -> bool:
    return _count(_key(kind, value), HOUR) <= limit


def request_password_reset(email, ip) -> None:
    """Someone asked for a password-reset link for `email`. Every active account with that address (any letter case) of
    an active facility gets one email: a pending invitation gets a fresh invitation (it sets the first password), an
    account with a password gets a reset link, anything else gets nothing. Returns nothing and raises nothing, so the
    caller cannot learn whether an account exists."""
    email = (email or "").strip()
    if not email:
        return
    if ip and not _allowed("reset-ip", str(ip), settings.PASSWORD_RESET_MAX_PER_IP_PER_HOUR):
        return
    if not _allowed("reset-address", email.lower(), settings.PASSWORD_RESET_MAX_PER_HOUR):
        return
    for user in User.objects.filter(email__iexact=email, is_active=True).select_related("tenant", "role").order_by("pk"):
        if not can_reset(user):
            continue
        if invitations.is_pending(user):
            try:
                invitations.send_invitation(user)
            except ValidationError:  # not pending after all (changed since the check); nothing to send
                pass
        elif user.has_usable_password():
            send_password_reset(user)  # sent to the stored address, never the typed one
