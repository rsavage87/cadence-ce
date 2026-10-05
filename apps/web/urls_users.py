"""Mounted at /users/ inside the `web` namespace. Keep the names `users`, `roles`, `user_invite`, `role_new`, `change_log`; the tabs partial
links to them."""
from django.urls import path

from . import views_change_log as log
from . import views_users as v

urlpatterns = [
    path("", v.users, name="users"),
    path("invite/", v.user_invite, name="user_invite"),
    path("<int:pk>/role/", v.user_role, name="user_role"),
    path("<int:pk>/edit/", v.user_edit, name="user_edit"),
    path("<int:pk>/deactivate/", v.user_deactivate, name="user_deactivate"),
    path("<int:pk>/reactivate/", v.user_reactivate, name="user_reactivate"),
    path("<int:pk>/resend-invite/", v.user_resend_invite, name="user_resend_invite"),
    path("roles/", v.roles, name="roles"),
    path("roles/new/", v.role_new, name="role_new"),
    path("roles/<uuid:pk>/level/", v.role_level, name="role_level"),
    path("roles/<uuid:pk>/scope/", v.role_scope, name="role_scope"),
    # Slice 20, part B: the Change log tab, its CSV, and its printable page
    path("log/", log.change_log_view, name="change_log"),
    path("log/export.csv", log.change_log_csv, name="change_log_csv"),
    path("log/print/", log.change_log_print, name="change_log_print"),
]
