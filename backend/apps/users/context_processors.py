"""Флаги для шапки: один context processor вместо инлайн-проверок ролей в шаблонах."""

from apps.users.models import UserRole
from apps.users.roles import staff_can_manage_catalog


def ui(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"is_sys_admin": False, "can_manage_catalog": False, "admin_laboratories": ()}
    is_sys_admin = user.role == UserRole.SYS_ADMIN
    return {
        "is_sys_admin": is_sys_admin,
        "can_manage_catalog": staff_can_manage_catalog(user),
        # Селектор лаборатории сисадмина в шапке: список всех лабораторий.
        "admin_laboratories": (
            _admin_laboratories() if is_sys_admin else ()
        ),
    }


def _admin_laboratories():
    from apps.scheduling.models import Laboratory

    return Laboratory.objects.select_related("training_center").order_by("training_center__number", "name")
