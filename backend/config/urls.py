from django.contrib import admin
from django.urls import include, path

from config.views import health

urlpatterns = [
    path("", include("apps.users.urls")),
    path("", include("apps.bookings.urls")),
    path("lab-head/", include("apps.scheduling.urls")),
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
]
