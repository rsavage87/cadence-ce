"""Mounted at /settings/ inside the `web` namespace. `web:settings` is the page; the shell's nav links to it."""
from django.urls import path

from . import views_settings as v

urlpatterns = [
    path("", v.settings_page, name="settings"),
]
