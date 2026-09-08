from django.urls import path

from apps.users.views import HomeView, WebLoginView, WebLogoutView

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("login/", WebLoginView.as_view(), name="login"),
    path("logout/", WebLogoutView.as_view(), name="logout"),
]
