"""Mounted at /contracts/ inside the `web` namespace. Keep the names `contracts`, `contract`, `contract_new`; the shell and other screens link to them."""
from django.urls import path

from . import views_contracts as v

urlpatterns = [
    path("", v.contracts, name="contracts"),
    path("new/", v.contract_new, name="contract_new"),
    path("assets/<str:tag>/support/", v.asset_support, name="asset_support"),
    path("<uuid:pk>/", v.contract_detail, name="contract"),
    path("<uuid:pk>/devices/", v.contract_devices, name="contract_devices"),
    path("<uuid:pk>/save/", v.contract_save, name="contract_save"),
    path("<uuid:pk>/renew/", v.contract_renew, name="contract_renew"),
    path("<uuid:pk>/delete/", v.contract_delete, name="contract_delete"),
    path("<uuid:pk>/add/", v.contract_add, name="contract_add"),
    path("<uuid:pk>/add-model/", v.contract_add_model, name="contract_add_model"),
    path("<uuid:pk>/remove/", v.contract_remove, name="contract_remove"),
]
