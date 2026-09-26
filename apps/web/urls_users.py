"""Mounted at /users/ inside the `web` namespace. Keep the names `users`, `roles`, `user_invite`, `role_new`; the tabs partial links to them."""
from django.urls import path

from . import views_users as v

urlpatterns = [
    path("", v.users, name="users"),
    path("invite/", v.user_invite, name="user_invite"),
    path("<int:pk>/role/", v.user_role, name="user_role"),
    path("<int:pk>/deactivate/", v.user_deactivate, name="user_deactivate"),
    path("<int:pk>/reactivate/", v.user_reactivate, name="user_reactivate"),
    path("roles/", v.roles, name="roles"),
    path("roles/new/", v.role_new, name="role_new"),
    path("roles/<uuid:pk>/level/", v.role_level, name="role_level"),
]
