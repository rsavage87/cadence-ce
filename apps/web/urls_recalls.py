"""Mounted at /recalls/ inside the `web` namespace. Keep these names; the shell, the device drawer, and the Overview link to them.

`web:recalls` accepts `?match=<AlertMatch id>` to expand and scroll to one card, and `?view=` for the pill groups."""
from django.urls import path

from . import views_recalls as v

urlpatterns = [
    path("", v.recalls, name="recalls"),
    path("match/", v.recall_match, name="recall_match"),
    path("<uuid:pk>/status/", v.recall_status, name="recall_status"),
    path("<uuid:pk>/work-orders/", v.recall_work_orders, name="recall_work_orders"),
]
