"""Users and access: Technician credentials tab (slice 5). Stub until the screen is built; keeps the nav and URL names resolvable."""
from django.shortcuts import render

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.USERS, Level.VIEW)
def credentials(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})


@web_view(Module.USERS, Level.EDIT)
def credential_new(request):
    return render(request, "web/_stub.html", {"nav_active": "users", "title": "Users and access"})
