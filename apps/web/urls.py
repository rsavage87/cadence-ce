from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views, views_account, views_equipment, views_invite

app_name = "web"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("login/", views_account.sign_in, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Signing in without a password yet (slice 10): reset links, the signed-in password change, and invitation links.
    path("password-reset/", views_account.password_reset, name="password_reset"),
    path("password-reset/sent/", views_account.password_reset_sent, name="password_reset_sent"),
    path("password-reset/done/", views_account.password_reset_complete, name="password_reset_complete"),
    path("password-reset/<uidb64>/<token>/", views_account.password_reset_confirm, name="password_reset_confirm"),
    path("account/password/", views_account.password_change, name="password_change"),
    path("invite/<uidb64>/<token>/", views_invite.invite_accept, name="invite_accept"),
    path("search/", views.search, name="search"),
    path("search/assets/", views.asset_search, name="asset_search"),
    path("equipment/", views.equipment, name="equipment"),
    path("equipment/new/", views_equipment.asset_new, name="asset_new"),  # before the tag route; "new" is a reserved tag
    path("equipment/<str:tag>/", views.asset_detail, name="asset"),
    path("equipment/<str:tag>/edit/", views_equipment.asset_edit, name="asset_edit"),
    path("equipment/<str:tag>/status/", views_equipment.asset_status, name="asset_status"),
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
    path("export/", include("apps.web.urls_exports")),
    path("print/", include("apps.web.urls_print")),
]
