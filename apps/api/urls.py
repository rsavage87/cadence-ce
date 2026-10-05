"""/api/v1/. The core viewsets (devices, models, departments, work orders) and Settings live in views.py; each area's module
registers its own routes (slice 19), so adding an endpoint never touches another area's file."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views, views_contracts, views_history, views_notifications, views_pm, views_recalls, views_reports, views_scan, views_users, views_work

router = DefaultRouter()
router.APIRootView = views.APIRootView
router.register("departments", views.DepartmentViewSet, basename="department")
router.register("device-models", views.DeviceModelViewSet, basename="devicemodel")
router.register("assets", views.AssetViewSet, basename="asset")
router.register("work-orders", views.WorkOrderViewSet, basename="workorder")
for area in (views_work, views_contracts, views_users, views_recalls, views_reports, views_pm, views_scan, views_history, views_notifications):
    area.register(router)

urlpatterns = router.urls + [
    path("settings/", views.FacilitySettingsView.as_view(), name="facility-settings"),
    path("settings/reset-policy/", views.ResetPolicyView.as_view(), name="facility-settings-reset-policy"),
]
