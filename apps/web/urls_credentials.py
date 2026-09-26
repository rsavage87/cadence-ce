"""Mounted at /users/credentials/ inside the `web` namespace. Keep the names `credentials` and `credential_new`; the tabs partial links to them."""
from django.urls import path

from . import views_credentials as v

urlpatterns = [
    path("", v.credentials, name="credentials"),
    path("new/", v.credential_new, name="credential_new"),
    path("<uuid:pk>/renew/", v.credential_renew, name="credential_renew"),
    path("<uuid:pk>/sign-off/", v.credential_sign_off, name="credential_sign_off"),
    path("<uuid:pk>/remove/", v.credential_remove, name="credential_remove"),
]
