"""Вход/выход и главная — каркас web-слоя (день 5).

Роли проверяются миксинами (mixins.py); главная не редиректит по роли,
а собирает плитки ссылок, чтобы отказы RoleRequiredMixin вели на неё без циклов.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.views.generic import TemplateView

from apps.users.forms import EmailAuthenticationForm
from apps.users.models import UserRole
from apps.users.roles import staff_can_manage_catalog


class WebLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


class WebLogoutView(LogoutView):
    next_page = "login"


class HomeView(LoginRequiredMixin, TemplateView):
    template_name = "home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["is_student"] = user.role == UserRole.STUDENT
        context["is_portal_manager"] = staff_can_manage_catalog(user)
        return context
