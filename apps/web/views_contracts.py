"""Contracts screen (slice 5). Stub until the screen is built; keeps the nav and URL names resolvable."""
from django.shortcuts import render

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.CONTRACTS, Level.VIEW)
def contracts(request):
    return render(request, "web/_stub.html", {"nav_active": "contracts", "title": "Service contracts"})


@web_view(Module.CONTRACTS, Level.VIEW)
def contract_detail(request, pk):
    return render(request, "web/_stub.html", {"nav_active": "contracts", "title": "Service contracts"})


@web_view(Module.CONTRACTS, Level.EDIT)
def contract_new(request):
    return render(request, "web/_stub.html", {"nav_active": "contracts", "title": "Service contracts"})
