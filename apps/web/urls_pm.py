"""Mounted at /pm/ inside the `web` namespace. `web:pm` is the schedule (?y=&m= for the month, ?day=YYYY-MM-DD for the
selected day); the nav and the Overview's PM tile link to it."""
from django.urls import path

from . import views_pm as v

urlpatterns = [
    path("", v.pm_schedule, name="pm"),
]
