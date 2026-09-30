"""
Signing in and passwords (slice 10): the sign-in page, password reset by email, and the signed-in password change.
Scaffold: agent B replaces these stubs; the names are what apps/web/urls.py routes to.
"""
from django.contrib.auth import views as auth_views
from django.http import Http404

sign_in = auth_views.LoginView.as_view(template_name="web/login.html", redirect_authenticated_user=True)


def password_reset(request):
    raise Http404


def password_reset_sent(request):
    raise Http404


def password_reset_complete(request):
    raise Http404


def password_reset_confirm(request, uidb64, token):
    raise Http404


def password_change(request):
    raise Http404
