from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("search/", views.search, name="search"),
    path("search/assets/", views.asset_search, name="asset_search"),
    path("equipment/", views.equipment, name="equipment"),
    path("equipment/<str:tag>/", views.asset_detail, name="asset"),
    path("work-orders/", views.workorders, name="workorders"),
    path("work-orders/new/", views.wo_new, name="wo_new"),
    path("work-orders/<str:number>/", views.wo_detail, name="wo"),
    path("work-orders/<str:number>/status/", views.wo_status, name="wo_status"),
    path("work-orders/<str:number>/assign/", views.wo_assign, name="wo_assign"),
    path("work-orders/<str:number>/notes/", views.wo_note, name="wo_note"),
    path("pm/", include("apps.web.urls_pm")),
    path("contracts/", include("apps.web.urls_contracts")),
    path("users/", include("apps.web.urls_users")),
    path("users/credentials/", include("apps.web.urls_credentials")),
    path("recalls/", include("apps.web.urls_recalls")),
    path("reports/", include("apps.web.urls_reports")),
    path("settings/", include("apps.web.urls_settings")),
]
