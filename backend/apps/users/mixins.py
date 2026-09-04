"""RoleRequiredMixin'ы: проверка роли в одном месте, ноль инлайн-проверок в views.

Отказ — flash-сообщение и редирект на главную (как в v1).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect

from apps.users import roles
from apps.users.models import UserRole


class RoleRequiredMixin(LoginRequiredMixin):
    denied_message = "Недостаточно прав."

    def has_access(self, user) -> bool:
        raise NotImplementedError

    def dispatch(self, request, *args, **kwargs):
        if not self.has_access(request.user):
            messages.error(request, self.denied_message)
            return redirect("home")
        return super().dispatch(request, *args, **kwargs)


class StudentRequiredMixin(RoleRequiredMixin):
    denied_message = "Доступ только для студентов."

    def has_access(self, user) -> bool:
        return user.is_authenticated and user.role == UserRole.STUDENT


class StaffRequiredMixin(RoleRequiredMixin):
    denied_message = "Доступ только для сотрудников лаборатории."

    def has_access(self, user) -> bool:
        return roles.is_staff_user(user)


class LabHeadRequiredMixin(RoleRequiredMixin):
    denied_message = "Доступ только для заведующего лабораторией."

    def has_access(self, user) -> bool:
        return roles.can_access_lab_head_portal(user)


class SysAdminRequiredMixin(RoleRequiredMixin):
    denied_message = "Доступ только для системного администратора."

    def has_access(self, user) -> bool:
        return user.is_authenticated and user.role == UserRole.SYS_ADMIN
