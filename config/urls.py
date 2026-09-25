from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.api.urls")),
    path("r/", include("apps.portal.urls")),
    path("", include("apps.web.urls")),
]
