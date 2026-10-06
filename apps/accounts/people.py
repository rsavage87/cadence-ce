"""
One person in several facilities (slice 22).

A person keeps one account per facility (User: its tenant, role, company, unit, API tokens, preferences, history), and the accounts
that share `User.person` are the same person. Every rule of "an account belongs to one facility" stays; this module only says which
of a person's accounts they may open from the one they are signed in to.

- Linking: only an invitation links accounts (apps.accounts.services), on the exact stored email of one other facility's account,
  never a superuser's. Nothing tells the inviting facility that the address had an account elsewhere.
- One password: User.save shares a newly set password with the person's other accounts (apps.accounts.models.share_password).
- Joining: a linked account starts as a pending invitation (no usable password). The person joins it by choosing it in the
  facility menu, or from the invitation email's join page, while signed in to another of their accounts: `join` copies the
  password they signed in with onto it, and the switch signs them in to it. Nothing joins on its own.
- Sign-in lands on the person's joined account that can sign in and was used last (`landing_account`), so a person deactivated in
  one facility still signs in to the others.
- Reading another facility: everything here reads system tables only (User, Tenant). Never a role: Role is tenant-scoped, and
  under row-level security another facility's roles are hidden from a request working in this one. Code that needs an account's
  role or levels in its facility enters `tenant_context(account.tenant)` and loads the account fresh there (see `in_facility`).
"""
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX

from apps.tenants.context import tenant_context

from .models import User


def can_enter(account) -> bool:
    """A facility account that may sign in at all: active, of an active facility, and not a platform superuser."""
    return bool(account.is_active and account.tenant_id and not account.is_superuser and account.tenant.is_active)


def is_pending(account) -> bool:
    """An invitation not yet accepted or joined: invited, never signed in, and no usable password."""
    return bool(account.is_invited and account.last_login is None and not account.has_usable_password())


def is_joined(account) -> bool:
    """An account its person can open now: it may sign in, has the person's password, and is not a pending invitation."""
    return can_enter(account) and account.has_usable_password() and not (account.is_invited and account.last_login is None)


def accounts_of(account):
    """The person's accounts in every facility (this one included), by facility name; just this account when it has no person.
    Includes deactivated accounts and facilities: callers filter with can_enter / is_joined / is_pending."""
    qs = User._default_manager.select_related("tenant")
    qs = qs.filter(person=account.person) if account.person else qs.filter(pk=account.pk)
    return qs.order_by("tenant__name", "pk")


def others(account) -> list:
    """The person's accounts in their other facilities."""
    return [a for a in accounts_of(account) if a.pk != account.pk] if account.person else []


def can_sign_in(account) -> bool:
    """Whether `account`'s person can already sign in: one of their accounts has a usable password and may sign in. A new
    facility's invitation then asks them to sign in as usual and join, never to set a password."""
    return any(can_enter(a) and a.has_usable_password() for a in accounts_of(account))


def landing_account(account):
    """The account a sign-in as `account` opens. With no person, `account` itself. For a person, their joined account that can
    sign in and was used last (then the oldest); None when no account of theirs can sign in now."""
    if not account.person:
        return account
    joined = [a for a in accounts_of(account) if is_joined(a)]
    if not joined:
        return None
    return sorted(joined, key=lambda a: (a.last_login is None, -(a.last_login.timestamp() if a.last_login else 0), a.pk))[0]


def facility_menu(user) -> list[dict]:
    """The facilities `user`'s person can open, for the top bar's facility menu and the account menu: this one (current), each
    other facility they have joined, and each invitation they can accept ("invited"). Empty unless there is somewhere to go."""
    if not user.person:
        return []
    rows = []
    for a in accounts_of(user):
        current = a.pk == user.pk
        if not current and not (is_joined(a) or (can_enter(a) and is_pending(a))):
            continue
        rows.append({"account": a.pk, "name": a.tenant.name, "slug": a.tenant.slug, "current": current, "invited": not current and is_pending(a)})
    return rows if len(rows) > 1 else []


def joined_count(user) -> int:
    """How many facilities the person has joined that they can open now (this one included): "All facilities" needs two."""
    return sum(1 for a in accounts_of(user) if a.pk == user.pk or is_joined(a)) if user.person else 1


def switch_target(user, account_id):
    """The account `account_id` names when `user` may switch to it now: another account of the same person (never one of
    another person, a superuser's, or one with no facility) that may sign in and is joined or a pending invitation. Else None."""
    try:
        pk = int(account_id)
    except (TypeError, ValueError):
        return None
    if not user.person or pk == user.pk:
        return None
    target = User._default_manager.select_related("tenant").filter(pk=pk, person=user.person).first()
    if target is None or not can_enter(target) or not (is_joined(target) or is_pending(target)):
        return None
    return target


def join(user, target) -> bool:
    """Accept `target`'s pending invitation for `user`'s person: copy the password `user` signed in with onto it, in one
    conditional UPDATE (so it never undoes a concurrent deactivation, and joins only an invitation still pending). False when
    it no longer is one. The caller signs the person in to `target` (login), which records the first sign-in."""
    if not (user.person and user.has_usable_password()):
        return False
    return bool(User._default_manager.filter(
        pk=target.pk, person=user.person, tenant__isnull=False, is_superuser=False, is_active=True, is_invited=True,
        last_login__isnull=True, password__startswith=UNUSABLE_PASSWORD_PREFIX,
    ).update(password=user.password))


@contextmanager
def in_facility(account):
    """Work inside `account`'s facility with the account loaded fresh there, so its role and levels read that facility's rows
    (row-level security hides them from any other). Yields the fresh account."""
    with tenant_context(account.tenant):
        yield User._default_manager.select_related("tenant").get(pk=account.pk)


def with_facility(url: str, tenant) -> str:
    """`url` with `facility=<slug>` added to its query: links in staff emails name the facility they are for, since a person may
    work in several and record numbers (WO-26-0042) repeat across facilities (apps.web.decorators answers a link for another one)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "facility"] + [("facility", tenant.slug)]
    return urlunsplit(parts._replace(query=urlencode(query)))
