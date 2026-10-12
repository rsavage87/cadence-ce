"""Mounted at /contracts/vendors/ inside the `web` namespace (slice 31): the Contracts screen's Vendor staff tab. Keep these names: the
change log and the survey binder's gaps link to them (`web:vendor_staff?vendor=<name>` filters the tab to a vendor). `web:vendor_engineer`
takes the engineer's id and opens their drawer, or a full page when opened directly. The rest are the modals (Add engineer, Change
engineer, Add training, Add attestation) and the removals of a record entered in error."""
from django.urls import path

from . import views_vendors as v

urlpatterns = [
    path("", v.vendor_staff, name="vendor_staff"),
    path("engineers/new/", v.engineer_new, name="vendor_engineer_new"),
    path("engineers/<uuid:pk>/", v.vendor_engineer, name="vendor_engineer"),
    path("engineers/<uuid:pk>/change/", v.engineer_change, name="vendor_engineer_change"),
    path("trainings/new/", v.training_new, name="vendor_training_new"),
    path("trainings/<uuid:pk>/remove/", v.training_remove, name="vendor_training_remove"),
    path("attestations/new/", v.attestation_new, name="vendor_attestation_new"),
    path("attestations/<uuid:pk>/remove/", v.attestation_remove, name="vendor_attestation_remove"),
]
