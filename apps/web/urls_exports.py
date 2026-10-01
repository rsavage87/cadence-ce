"""Mounted at /export/ inside the `web` namespace (slice 11): CSV downloads of the lists, with the list's filters."""
from django.urls import path

from . import views_exports as v

urlpatterns = [
    path("equipment.csv", v.equipment_csv, name="equipment_csv"),
    path("work-orders.csv", v.workorders_csv, name="workorders_csv"),
    path("contracts.csv", v.contracts_csv, name="contracts_csv"),
    path("contracts/<uuid:pk>/devices.csv", v.contract_devices_csv, name="contract_devices_csv"),
]
