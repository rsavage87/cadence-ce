"""Mounted at /users/credentials/ inside the `web` namespace. Keep the names `credentials` and `credential_new`; the tabs partial links to them."""
from django.urls import path

from . import views_credentials as v

urlpatterns = [
    path("", v.credentials, name="credentials"),
    path("new/", v.credential_new, name="credential_new"),
]
