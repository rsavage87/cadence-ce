"""
Sign-in backend: the username or the email address, in any letter case.

Invited accounts use the lowercased work email as their username, but people type addresses the way they like. A login
that names more than one account without being an exact username (two accounts differing only in case) signs in to none of
them; the exact username still works.

Slice 22: a login that names one person (any of their accounts, or several accounts that all share `person`) signs in to the
person's landing account (apps.accounts.people.landing_account): their joined account that can sign in and was used last. A
person deactivated in one facility still signs in to the others.

Every password check runs through here (the sign-in page and Admin's), so this is also where the lockouts of
apps.accounts.signin are enforced and counted.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.core.http import client_ip

from . import people, signin

User = get_user_model()


MATCHES = 50  # more accounts than this under one login is never one person


def find_account(login: str):
    """The account a sign-in as `login` opens: the account it names (the exact username, else the only account, or the only
    person, whose username or email matches it in any letter case), or for a person their landing account. Surrounding
    whitespace is ignored. None when it names nobody, several people, or a person none of whose accounts can sign in now."""
    login = (login or "").strip()
    if not login:
        return None
    exact = User.objects.select_related("tenant").filter(username=login).first()
    if exact is not None:
        return people.landing_account(exact)
    matches = list(User.objects.select_related("tenant").filter(Q(username__iexact=login) | Q(email__iexact=login))[:MATCHES])
    persons = {m.person for m in matches}
    if len(matches) == 1 or (len(persons) == 1 and None not in persons and len(matches) < MATCHES):
        return people.landing_account(matches[0])
    return None


def facility_is_active(user) -> bool:
    """No facility (a platform superuser), or an active one. Signing in, every request's user load, and API tokens
    (apps.api.authentication) all check it."""
    return user.tenant_id is None or user.tenant.is_active


class UsernameOrEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None
        username = username.strip()
        ip = client_ip(request) if request is not None else None
        if signin.is_locked(username, ip):
            raise PermissionDenied  # stop here: no other backend may accept a locked login either
        user = find_account(username)
        if user is None:
            User().set_password(password)  # same hashing cost as a real account, so timing does not say whether it exists
        elif user.check_password(password) and self.user_can_authenticate(user):
            signin.clear_failures(username, user)
            return user
        signin.record_failure(username, ip)
        return None

    def get_user(self, user_id):
        """Every request loads the signed-in user, with the facility (the middleware reads it). Not the role: this runs before
        the middleware sets the tenant, and Role is tenant-scoped, so under row-level security a join here would find no role
        (or fail) and the user would have no access. The role loads lazily once the tenant is set."""
        try:
            user = User._default_manager.select_related("tenant").get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None

    def user_can_authenticate(self, user):
        """Active, and (unless the account belongs to no facility) its facility is active. Also checked on every request."""
        return super().user_can_authenticate(user) and facility_is_active(user)
