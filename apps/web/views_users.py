"""Users and access: Users and Roles tabs (slice 5). Stub until the screen is built; keeps the nav and URL names resolvable."""
from django.shortcuts import render

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.USERS, Level.VIEW)
def users(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})


@web_view(Module.USERS, Level.VIEW)
def roles(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})


@web_view(Module.USERS, Level.FULL)
def user_invite(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})


@web_view(Module.USERS, Level.FULL)
def role_new(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})
