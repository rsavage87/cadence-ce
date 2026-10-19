"""Mounted at /test-equipment/ inside the `web` namespace (slice 35): the CE department's test equipment, the Equipment screen's second
tab (nav_active "equipment"; never under /equipment/<tag>/, which would reserve a tag). Keep these names: the binder, the change log,
and the work order's drawer link to them. `web:test_equipment_item` takes the item's tag (any letter case) and opens its drawer, or its
page when opened directly. Calibrations are named by their id."""
from django.urls import path

from . import views_testequipment as v

urlpatterns = [
    path("", v.test_equipment, name="test_equipment"),
    path("new/", v.test_equipment_new, name="test_equipment_new"),
    path("export.csv", v.test_equipment_csv, name="test_equipment_csv"),
    path("<str:tag>/", v.test_equipment_item, name="test_equipment_item"),
    path("<str:tag>/edit/", v.test_equipment_edit, name="test_equipment_edit"),
    path("<str:tag>/status/", v.test_equipment_status, name="test_equipment_status"),
    path("<str:tag>/uses.csv", v.test_equipment_uses_csv, name="test_equipment_uses_csv"),
    path("<str:tag>/calibrations/new/", v.test_equipment_calibrate, name="test_equipment_calibrate"),
    path("<str:tag>/calibrations/<uuid:pk>/error/", v.test_equipment_calibration_error, name="test_equipment_calibration_error"),
    path("<str:tag>/calibrations/<uuid:pk>/review/", v.test_equipment_review, name="test_equipment_review"),
    path("<str:tag>/calibrations/<uuid:pk>/review.csv", v.test_equipment_review_csv, name="test_equipment_review_csv"),
]
