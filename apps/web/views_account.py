"""
Signing in and passwords (slice 10): the sign-in page, password reset by email, and the signed-in password change.

The signed-out pages extend web/auth_base.html (no app shell, no Referer to other sites). None of them says whether an account exists:
a reset request always lands on the same "if an account uses that address" page, and lockouts (apps.accounts.signin) are
keyed on the typed login, not on an account. Reset links carry their token only until the first request, which moves it
into the session and redirects to a ".../set-password/" URL (Django's PasswordResetConfirmView).
"""
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts import signin
from apps.accounts.models import User
from apps.core.http import client_ip

from .decorators import web_view
from .forms_account import ChangePasswordForm, NewPasswordForm, PasswordResetRequestForm, SignInForm


class SignInView(auth_views.LoginView):
    template_name = "web/login.html"
    redirect_authenticated_user = True
    authentication_form = SignInForm


sign_in = SignInView.as_view()


# --- password reset ---------------------------------------------------------------------------------

@never_cache
def password_reset(request):
    form = PasswordResetRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        signin.request_password_reset(form.cleaned_data["email"], client_ip(request))
        return redirect("web:password_reset_sent")  # the same answer whether or not the address has an account
    return render(request, "web/password_reset.html", {"form": form})


def password_reset_sent(request):
    return render(request, "web/password_reset_sent.html", {"valid_for": signin.reset_link_lifetime()})


def password_reset_complete(request):
    return render(request, "web/password_reset_complete.html")


class ResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "web/password_reset_confirm.html"
    form_class = NewPasswordForm
    token_generator = default_token_generator
    success_url = reverse_lazy("web:password_reset_complete")

    def get_user(self, uidb64):
        """Only an active account of an active facility (or of none) can be reset; anything else reads as a dead link."""
        try:
            user = User.objects.select_related("tenant").filter(pk=int(urlsafe_base64_decode(uidb64).decode())).first()
        except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
            return None
        return user if user is not None and signin.can_reset(user) else None

    def form_valid(self, form):
        response = super().form_valid(form)
        signin.clear_failures(form.user.username, form.user)  # the lockout was for whoever did not know the password
        return response

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "valid_for": signin.reset_link_lifetime()}


password_reset_confirm = ResetConfirmView.as_view()


# --- change password (signed in) -----------------------------------------------------------------------

@sensitive_post_parameters()
@never_cache
@web_view(scoped=True)  # shows nothing of the facility, so a scoped user (slice 16) changes their password too
def password_change(request):
    """Any signed-in user of a facility. Keeps this session signed in; Django signs every other session out, since each
    session carries a hash of the password."""
    if request.method == "POST":
        form = ChangePasswordForm(request.user, request.POST, request=request)
        if form.is_valid():
            form.save()
            update_session_auth_hash(request, form.user)
            return redirect(reverse("web:password_change") + "?changed=1")
    else:
        form = ChangePasswordForm(request.user, request=request)
    return render(request, "web/account_password.html", {"form": form, "changed": request.method == "GET" and request.GET.get("changed") == "1"})
