"""Recalls and alerts screen (slice 6). Stub until the screen is built; keeps the nav and URL names resolvable."""
from django.shortcuts import render

from apps.accounts.models import Level, Module

from .decorators import web_view


@web_view(Module.RECALLS, Level.VIEW)
def recalls(request):
    return render(request, "web/_stub.html", {"nav_active": "recalls", "title": "Recalls and alerts"})


@web_view(Module.RECALLS, Level.EDIT)
def recall_status(request, pk):
    return render(request, "web/_stub.html", {"nav_active": "recalls", "title": "Recalls and alerts"})


@web_view(Module.RECALLS, Level.EDIT)
def recall_work_orders(request, pk):
    return render(request, "web/_stub.html", {"nav_active": "recalls", "title": "Recalls and alerts"})


@web_view(Module.RECALLS, Level.EDIT)
def recall_match(request):
    return render(request, "web/_stub.html", {"nav_active": "recalls", "title": "Recalls and alerts"})
