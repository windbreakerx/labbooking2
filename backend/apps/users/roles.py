"""Ролевые предикаты и миксины — единственное место сравнения ролей.

Сервисы используют предикаты отсюда; views — RoleRequiredMixin'ы (День 4).
"""

from apps.users.models import User, UserRole


def is_staff_user(user: User) -> bool:
    """Все сотрудники, включая преподавателя и сисадмина."""
    return user.role in {
        UserRole.LAB_ADMIN,
        UserRole.LAB_HEAD,
        UserRole.TEACHER,
        UserRole.SYS_ADMIN,
    }


def staff_can_modify_bookings(user: User) -> bool:
    """LAB_ADMIN, LAB_HEAD и SYS_ADMIN меняют статусы и пишут вручную; TEACHER — read-only."""
    return user.role in {
        UserRole.LAB_ADMIN,
        UserRole.LAB_HEAD,
        UserRole.SYS_ADMIN,
    }
