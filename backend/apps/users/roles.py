"""Ролевые предикаты и миксины — единственное место сравнения ролей.

Сервисы используют предикаты отсюда; views — RoleRequiredMixin'ы (mixins.py).
Предикаты безопасны для анонимного пользователя (возвращают False).
"""

from apps.users.models import UserRole

_MODIFY_BOOKINGS_ROLES = {
    UserRole.LAB_ADMIN,
    UserRole.LAB_HEAD,
    UserRole.SYS_ADMIN,
}
_CATALOG_ROLES = {
    UserRole.LAB_HEAD,
    UserRole.SYS_ADMIN,
}


def _roles_of(user) -> set:
    if not getattr(user, "is_authenticated", False):
        return set()
    return {user.role}


def is_staff_user(user) -> bool:
    """Все сотрудники, включая преподавателя и сисадмина."""
    return bool(_roles_of(user) & {UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.TEACHER, UserRole.SYS_ADMIN})


def staff_can_modify_bookings(user) -> bool:
    """LAB_ADMIN, LAB_HEAD и SYS_ADMIN меняют статусы и пишут вручную; TEACHER — read-only."""
    return bool(_roles_of(user) & _MODIFY_BOOKINGS_ROLES)


def staff_can_manage_catalog(user) -> bool:
    """CRUD каталога (дисциплины, ЛР, комнаты, стенды, привязки) — завлаб и сисадмин."""
    return bool(_roles_of(user) & _CATALOG_ROLES)


def is_lab_head_user(user) -> bool:
    return user.role == UserRole.LAB_HEAD if user.is_authenticated else False


def can_access_lab_head_portal(user) -> bool:
    """Завлаб или SYS_ADMIN (мониторинг всех учебных центров)."""
    return bool(_roles_of(user) & _CATALOG_ROLES)
