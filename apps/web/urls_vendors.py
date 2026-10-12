"""Mounted at /contracts/vendors/ inside the `web` namespace (slice 31): the Contracts screen's Vendor staff tab. Keep these names: the
change log and the survey binder's gaps link to them (`web:vendor_staff?vendor=<name>` filters the tab to a vendor). `web:vendor_engineer`
takes the engineer's id and opens their drawer, or a full page when opened directly."""
from django.urls import path

from . import views_vendors as v

urlpatterns = [
    path("", v.vendor_staff, name="vendor_staff"),
    path("engineers/<uuid:pk>/", v.vendor_engineer, name="vendor_engineer"),
]
