from rest_framework import authentication, exceptions

from apps.accounts.backends import facility_is_active


class TokenAuthentication(authentication.TokenAuthentication):
    """DRF's token check (the user is active) plus the sign-in rule: a user whose facility is deactivated cannot use the API either."""

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if not facility_is_active(user):
            raise exceptions.AuthenticationFailed("User inactive or deleted.")
        return user, token
