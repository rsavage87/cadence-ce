"""
Sign-in backend: the username or the email address, in any letter case.

Invited accounts use the lowercased work email as their username, but people type addresses the way they like. A login
that names more than one account without being an exact username (the same person at two facilities, or two accounts
differing only in case) signs in to none of them; the exact username still works.

Every password check runs through here (the sign-in page and Admin's), so this is also where the lockouts of
apps.accounts.signin are enforced and counted.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.core.http import client_ip

from . import signin

User = get_user_model()


def find_account(login: str):
    """The one account `login` names: the exact username, else the only account whose username or email matches it in
    any letter case. Surrounding whitespace is ignored."""
    login = (login or "").strip()
    if not login:
        return None
    exact = User.objects.filter(username=login).first()
    if exact is not None:
        return exact
    matches = list(User.objects.filter(Q(username__iexact=login) | Q(email__iexact=login))[:2])
    return matches[0] if len(matches) == 1 else None


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
        """Every request loads the signed-in user; bring the facility and role along, since the middleware and shell read both."""
        try:
            user = User._default_manager.select_related("tenant", "role").get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None

    def user_can_authenticate(self, user):
        """Active, and (unless the account belongs to no facility) its facility is active. Also checked on every request."""
        if not super().user_can_authenticate(user):
            return False
        return user.tenant_id is None or user.tenant.is_active
