"""Mounted at /contracts/ inside the `web` namespace. Keep the names `contracts`, `contract`, `contract_new`; the shell and other screens link to them."""
from django.urls import path

from . import views_contracts as v

urlpatterns = [
    path("", v.contracts, name="contracts"),
    path("new/", v.contract_new, name="contract_new"),
    path("<uuid:pk>/", v.contract_detail, name="contract"),
]
