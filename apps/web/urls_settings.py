"""Mounted at /settings/ inside the `web` namespace. `web:settings` is the page; the shell's nav links to it."""
from django.urls import path

from . import views_settings as v

urlpatterns = [
    path("", v.settings_page, name="settings"),
    path("portal/", v.settings_portal, name="settings_portal"),
    path("portal/links/", v.settings_dept_links, name="settings_dept_links"),
    path("policy/", v.settings_policy, name="settings_policy"),
    path("policy/reset/", v.settings_policy_reset, name="settings_policy_reset"),
    path("targets/", v.settings_targets, name="settings_targets"),
    path("time-zone/", v.settings_time_zone, name="settings_time_zone"),  # slice 21: the facility's time zone
]
