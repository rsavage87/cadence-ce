"""
Sign-in lockouts and password-reset requests (slice 10).

Lockouts count failed sign-ins in the Django cache under two keys: the login text as typed (case-folded and stripped) and
the client address. The login key is never an account: a lockout for a name nobody uses looks exactly like one for a real
account, so neither the message nor the timing says whether the account exists. Each key has a fixed window of
SIGNIN_WINDOW_MINUTES that starts at its first failure; reaching SIGNIN_MAX_FAILURES (or SIGNIN_MAX_FAILURES_PER_IP for
the address) refuses every sign-in on that key, correct password or not, until the window ends. The backend enforces it
(apps.accounts.backends), so every password check (the sign-in page and Admin's) is covered; the sign-in form checks
first only to say why.

Password reset: request_password_reset never tells its caller anything. Requests beyond the per-email-address and
per-caller limits are dropped silently, and an unknown, deactivated, or closed-facility address is answered exactly like
a real one: the page always says "if an account uses that address, a link is on its way".

One person in several facilities (slice 22, apps.accounts.people): a person's accounts share one password, so they share one
lockout too. Once the backend has resolved a login to a person, failures also count under the person (`person:<uuid>`), and
while that count is over the limit every password is refused, so each facility's username adds no guessing budget of its own;
the Change password form counts and checks the same key. A reset request sends a person who can sign in one link, for the
account a sign-in opens (people.landing_account); setting the password there sets it for every facility.
"""
import hashlib
import math
import time

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.connection import ConnectionProxy
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from . import emails, invitations, people
from .models import User

HOUR = 60 * 60
# A store of its own, large enough that junk logins cannot evict real counters (settings.CACHES). A proxy, like
# django.core.cache.cache, so each thread uses its own connection if the store is ever shared (Redis).
cache = ConnectionProxy(caches, "limits")


def _norm(login) -> str:
    """One key per login however it is typed. upper(), not lower(): Postgres compares iexact with UPPER, which folds a
    dotless 'ı' to 'I' (Python's lower() keeps it), so 'kım@...' names kim's account and must share kim's counter."""
    return (login or "").strip().upper()


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


def _person_key(user) -> str | None:
    """The person's counter (slice 22): one for all of their accounts, whichever facility's username or email was typed."""
    return _key("person", str(user.person)) if user is not None and user.person else None


def person_locked(user) -> str | None:
    """Why every password for `user`'s person is refused now, or None (always None for an account in one facility: its login
    texts are counted as typed). Checked once a login has been resolved to the person (the backend, Change password), so the
    message is the one a locked login gets."""
    key = _person_key(user)
    if key and _over(key, settings.SIGNIN_MAX_FAILURES):
        return f"Too many failed sign-ins. Try again in {_time_left(key, _window())} or reset your password."
    return None


def record_failure(login, ip, user=None) -> None:
    """Count a failed sign-in for the login text and the address, and for `user`'s person when the login named one."""
    login = _norm(login)
    if login:
        _count(_key("login", login), _window())
    if ip:
        _count(_key("ip", str(ip)), _window())
    key = _person_key(user)
    if key:
        _count(key, _window())


def clear_failures(login, user=None) -> None:
    """Forget the failures for `login`, and for `user`'s username, email, and person when given (a successful sign-in or a reset
    proves who they are, whichever name they were typed under). The address counter is left alone: one good sign-in from
    a shared network does not excuse the failures from it."""
    names = {_norm(login)}
    if user is not None:
        # Every account of the person (slice 22): a reset sent for the landing account must also free the username of the facility
        # where Change password counted the failures.
        for account in people.accounts_of(user):
            names |= {_norm(account.username), _norm(account.email)}
    keys = [_key("login", n) for n in names if n]
    person = _person_key(user)
    if person:
        keys.append(person)
    cache.delete_many([*keys, *(f"{k}:until" for k in keys)])


# --- password reset --------------------------------------------------------------------------------

def reset_link_lifetime() -> str:
    """How long a reset link works, in words, from PASSWORD_RESET_TIMEOUT ('2 hours')."""
    return _duration(settings.PASSWORD_RESET_TIMEOUT)


def password_reset_url(user) -> str:
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    return settings.APP_BASE_URL + reverse("web:password_reset_confirm", args=[uidb64, default_token_generator.make_token(user)])


def sign_in_name(user) -> str:
    """What `user` signs in with, as the emails say it: the email address, which opens the person's account used last (slice 22:
    a second facility's username, "<email>@<slug>", is never shown). Only where the address names several people, so that a
    sign-in by it opens none (apps.accounts.backends), the account's own username, which still does."""
    from .backends import find_account  # here: the backend imports this module

    if user.email:
        opened = find_account(user.email)
        if opened is not None and (opened.pk == user.pk or (user.person and opened.person == user.person)):
            return user.email
    return user.username


def facilities_opened(user) -> list[str]:
    """The facilities `user`'s password opens, by name: for a person, every facility they have joined and can sign in to (the
    password is theirs in all of them); else the account's own. Reads User and Tenant only."""
    if user.person:
        return [a.tenant.name for a in people.accounts_of(user) if people.is_joined(a)]
    return [user.tenant.name] if user.tenant_id else []


def in_words(names: list[str]) -> str:
    """'A', 'A and B', 'A, B, and C'."""
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def send_password_reset(user) -> bool:
    """Email `user` a link to set a new password. It works for PASSWORD_RESET_TIMEOUT and only once (the token hashes the
    password and the last sign-in). It says what to sign in with and, for a person in several facilities, that the password
    opens all of them, by name. False if the email could not be sent."""
    facilities = facilities_opened(user)
    context = {"user": user, "url": password_reset_url(user), "valid_for": reset_link_lifetime(),
               "facility": user.tenant.name if user.tenant_id else "", "sign_in": sign_in_name(user),
               "facilities": in_words(facilities) if len(facilities) > 1 else ""}
    return emails.send(user.email, "accounts/email/password_reset", context)


def can_reset(user) -> bool:
    """Active, and its facility (if it has one) is active: the only accounts a reset email or link is for."""
    return bool(user.is_active and (user.tenant_id is None or user.tenant.is_active))


def _allowed(kind: str, value: str, limit: int) -> bool:
    return _count(_key(kind, value), HOUR) <= limit


def _answer(user) -> None:
    """One account's email, as before slice 22: a pending invitation gets a fresh invitation (it sets the first password, or for
    a person who signs in elsewhere links to the join page), an account with a password a reset link, anything else nothing.
    The resend writes no access event: nobody on the staff changed anything, and the facility must not learn of the request."""
    if invitations.is_pending(user):
        try:
            invitations.send_invitation(user)  # no access event: see send_invitation
        except ValidationError:  # not pending after all (changed since the check); nothing to send
            pass
    elif user.has_usable_password():
        send_password_reset(user)  # sent to the stored address, never the typed one


def request_password_reset(email, ip) -> None:
    """Someone asked for a password-reset link for `email`. The active accounts with that address (any letter case) of
    active facilities are answered by person (slice 22): a person who can sign in gets one reset link, for the account a sign-in
    opens (people.landing_account), to its stored address, and their invitations still pending get nothing (they join those
    while signed in). Every other account, linked or not, gets its own email (_answer). Returns nothing and raises nothing, so
    the caller cannot learn whether an account exists."""
    email = (email or "").strip()
    if not email:
        return
    if ip and not _allowed("reset-ip", str(ip), settings.PASSWORD_RESET_MAX_PER_IP_PER_HOUR):
        return
    if not _allowed("reset-address", _norm(email), settings.PASSWORD_RESET_MAX_PER_HOUR):
        return
    # Not select_related("role"): this runs signed out, with no tenant set, and Role is tenant-scoped (row-level security).
    # User and Tenant only, here and in people's reads.
    accounts = [u for u in User.objects.filter(email__iexact=email, is_active=True).select_related("tenant").order_by("pk") if can_reset(u)]
    answered = set()
    for user in accounts:
        if user.person is None:
            _answer(user)
            continue
        if user.person in answered:
            continue
        answered.add(user.person)
        landing = people.landing_account(user)
        if landing is not None:
            send_password_reset(landing)
        else:  # nobody of theirs can sign in now: each account as before
            for account in accounts:
                if account.person == user.person:
                    _answer(account)
