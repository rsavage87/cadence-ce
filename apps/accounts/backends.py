"""
Sign-in backend: the username or the email address, in any letter case.

Invited accounts use the lowercased work email as their username, but people type addresses the way they like. An
address that matches more than one account (the same person at two facilities, or two accounts differing only in case)
signs in to none of them by the ambiguous route; the exact username still works.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


def find_account(login: str):
    """The one account `login` names: exact username first, then case-insensitive username, then case-insensitive email."""
    login = (login or "").strip()
    if not login:
        return None
    exact = User.objects.filter(username=login).first()
    if exact is not None:
        return exact
    for lookup in ("username__iexact", "email__iexact"):
        matches = list(User.objects.filter(**{lookup: login})[:2])
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


class UsernameOrEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None
        user = find_account(username)
        if user is None:
            User().set_password(password)  # same hashing cost as a real account, so timing does not say whether it exists
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
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
