from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django_ratelimit.decorators import ratelimit

from config.ratelimit import client_ip
from config.views import health

# У админки свой view входа: rate limit на WebLoginView его не покрывает.
# Ключ — реальный IP клиента (за nginx это X-Real-IP, а не REMOTE_ADDR).
# Лимит — только POST; GET страницы входа не ограничивается.
admin_login = ratelimit(key=client_ip, rate=settings.ADMIN_LOGIN_RATE, method="POST")(
    admin.site.login
)

urlpatterns = [
    path("", include("apps.users.urls")),
    path("", include("apps.academics.urls")),
    path("", include("apps.bookings.urls")),
    path("lab-head/", include("apps.scheduling.urls")),
    path("admin/login/", admin_login, name="admin-login"),
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
]
