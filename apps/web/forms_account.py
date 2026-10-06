"""
Forms for signing in and passwords (slice 10): Django's auth forms with the lockouts of apps.accounts.signin and plain
wording. The views are in views_account.py.
"""
from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm, SetPasswordMixin
from django.contrib.auth.password_validation import password_validators_help_texts
from django.core.exceptions import ValidationError
from django.utils.functional import cached_property
from django.views.decorators.debug import sensitive_variables

from apps.accounts import signin
from apps.core.http import client_ip

WRONG_LOGIN = "That email or username and password do not match."
MISMATCH = "The two new passwords do not match."


def _ip(request):
    return client_ip(request) if request is not None else None


class SignInForm(AuthenticationForm):
    """Refuses while the login text or the caller's address is locked out, whatever the password, and says so from the
    failure that locks it. The backend counts failures and clears them on success, so every sign-in path shares them."""

    error_messages = {"invalid_login": WRONG_LOGIN, "inactive": WRONG_LOGIN}

    @sensitive_variables()
    def clean(self):
        login = self.cleaned_data.get("username")
        if not (login and self.cleaned_data.get("password")):
            return super().clean()
        locked = signin.is_locked(login, _ip(self.request))
        if locked:
            raise ValidationError(locked, code="locked")
        try:
            return super().clean()
        except ValidationError:
            locked = signin.is_locked(login, _ip(self.request))  # this failure may be the one that locked it
            if locked:
                raise ValidationError(locked, code="locked")
            raise


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(label="Email", max_length=254,
                             error_messages={"required": "Enter your email address.", "invalid": "Enter a valid email address."})


class _NewPassword(forms.Form):
    """Plain labels, and the validators' rules as a list for the template (the confirmation gets no second help line).
    A Form, so its fields replace SetPasswordForm's in the subclasses below."""

    new_password1, new_password2 = SetPasswordMixin.create_password_fields(label1="New password", label2="Confirm new password")
    new_password2.help_text = ""

    @property
    def rules(self) -> list[str]:
        return password_validators_help_texts()


class NewPasswordForm(_NewPassword, SetPasswordForm):
    error_messages = {**SetPasswordForm.error_messages, "password_mismatch": MISMATCH}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].widget.attrs["autofocus"] = True


class ChangePasswordForm(_NewPassword, PasswordChangeForm):
    """Django's change form. A wrong current password counts as a failed sign-in for the account, and for its person (slice 22:
    the same count a sign-in by any of their facilities' usernames adds to), so a borrowed session cannot be used to guess it;
    while the account or the person is locked the form refuses. The password is the person's: changing it here changes it in
    every facility they work in (User.save), and the page names them (`facilities`)."""

    error_messages = {**PasswordChangeForm.error_messages, "password_mismatch": MISMATCH, "password_incorrect": "That is not your current password."}

    def __init__(self, user, *args, request=None, **kwargs):
        self.request = request
        super().__init__(user, *args, **kwargs)
        self.fields["old_password"].label = "Current password"

    @cached_property
    def sign_in(self) -> str:
        """What the person signs in with: their email (signin.sign_in_name), never a second facility's username."""
        return signin.sign_in_name(self.user)

    @cached_property
    def facilities(self) -> str:
        """The facilities this password opens, in words ("Lakeside Surgery Center and Riverside Regional"), when there are several."""
        names = signin.facilities_opened(self.user)
        return signin.in_words(names) if len(names) > 1 else ""

    @sensitive_variables("old_password")
    def clean_old_password(self):
        locked = signin.is_locked(self.user.username, _ip(self.request)) or signin.person_locked(self.user)
        if locked:
            raise ValidationError(locked, code="locked")
        try:
            return super().clean_old_password()
        except ValidationError:
            signin.record_failure(self.user.username, _ip(self.request), self.user)
            raise
