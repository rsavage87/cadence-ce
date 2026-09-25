from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("<slug:tenant_slug>/", views.request_form, name="request"),
    path("<slug:tenant_slug>/done/<str:number>/", views.request_done, name="done"),
]
