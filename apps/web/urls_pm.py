"""Mounted at /pm/ inside the `web` namespace. `web:pm` is the schedule (?y=&m= for the month, ?day=YYYY-MM-DD for the
selected day); the nav and the Overview's PM tile link to it. `web:pm_create` creates the PM work orders for one day
(the day is parsed in the view, so a malformed one is a 404). The PM program's routes (slice 14) are in urls_models."""
from django.urls import include, path

from . import views_pm as v

urlpatterns = [
    path("", v.pm_schedule, name="pm"),
    path("day/<str:day>/work-orders/", v.pm_create, name="pm_create"),
    path("", include("apps.web.urls_models")),
]
