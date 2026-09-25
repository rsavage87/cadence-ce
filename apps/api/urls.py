from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("departments", views.DepartmentViewSet, basename="department")
router.register("device-models", views.DeviceModelViewSet, basename="devicemodel")
router.register("assets", views.AssetViewSet, basename="asset")
router.register("work-orders", views.WorkOrderViewSet, basename="workorder")
router.register("contracts", views.ContractViewSet, basename="contract")
router.register("technicians", views.TechnicianViewSet, basename="technician")
router.register("credentials", views.CredentialViewSet, basename="credential")
router.register("alert-matches", views.AlertMatchViewSet, basename="alertmatch")
router.register("overview", views.OverviewViewSet, basename="overview")
router.register("pm", views.PmViewSet, basename="pm")

urlpatterns = router.urls
