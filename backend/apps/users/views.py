"""Вход/выход и главная — каркас web-слоя (день 5).

Роли проверяются миксинами (mixins.py); главная не редиректит по роли,
а собирает плитки ссылок, чтобы отказы RoleRequiredMixin вели на неё без циклов.
"""

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django_ratelimit.decorators import ratelimit

from apps.users.forms import EmailAuthenticationForm
from config.ratelimit import client_ip


@method_decorator(
    ratelimit(key=client_ip, rate=settings.LOGIN_RATE, method="POST"),
    name="post",
)
class WebLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


class WebLogoutView(LogoutView):
    next_page = "login"


class HomeView(LoginRequiredMixin, TemplateView):
    """Плитки главной: флаги ролей приходят из context processor ``users.ui``."""

    template_name = "home.html"
