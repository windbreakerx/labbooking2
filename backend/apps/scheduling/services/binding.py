"""Привязки дисциплин к лабораториям: факультетское правило + override для SYS_ADMIN.

«Своя» дисциплина завлаба: ``discipline.department.faculty == laboratory.faculty``
(оба факультета заданы). Несоответствующая привязка (``is_override``) — только
SYS_ADMIN и только с причиной; аудит живёт в самой строке LabDisciplineBinding.
"""

from django.db import IntegrityError, transaction

from apps.academics.models import Discipline
from apps.scheduling.models import LabDisciplineBinding, Laboratory
from apps.users.models import User, UserRole
from apps.users.roles import staff_can_manage_catalog


class BindingError(Exception):
    pass


def _faculty_matches(laboratory: Laboratory, discipline: Discipline) -> bool:
    """Дисциплина «своей» лаборатории: факультеты совпадают и оба заданы."""
    if not laboratory.faculty_id or not discipline.department_id:
        return False
    return Discipline.objects.filter(
        pk=discipline.pk, department__faculty_id=laboratory.faculty_id
    ).exists()


def can_bind(user: User, laboratory: Laboratory, discipline: Discipline) -> bool:
    """Завлаб — только дисциплины своего факультета; SYS_ADMIN — любые."""
    if not user.is_authenticated:
        return False
    if user.role == UserRole.SYS_ADMIN:
        return True
    if user.role != UserRole.LAB_HEAD:
        return False
    return _faculty_matches(laboratory, discipline)


def bindable_disciplines_qs(user: User, laboratory: Laboratory):
    """Кандидаты для привязки: завлабу — свой факультет, сисадмину — все."""
    qs = Discipline.objects.select_related("semester", "department").order_by("title")
    if user.is_authenticated and user.role == UserRole.SYS_ADMIN:
        return qs
    if not laboratory.faculty_id:
        return qs.none()
    return qs.filter(department__faculty_id=laboratory.faculty_id)


@transaction.atomic
def bind(
    user: User,
    laboratory: Laboratory,
    discipline: Discipline,
    *,
    override: bool = False,
    reason: str = "",
) -> LabDisciplineBinding:
    if not staff_can_manage_catalog(user):
        raise BindingError("Привязка доступна только заведующему лабораторией или сисадмину.")
    if override:
        if user.role != UserRole.SYS_ADMIN:
            raise BindingError(
                "Несоответствующую привязку может создать только системный администратор."
            )
        if not (reason or "").strip():
            raise BindingError("Для несоответствующей привязки укажите причину.")
    elif not _faculty_matches(laboratory, discipline):
        raise BindingError(
            "Дисциплина относится к другому факультету. Такая привязка доступна только "
            "системному администратору с указанием причины."
        )
    try:
        return LabDisciplineBinding.objects.create(
            laboratory=laboratory,
            discipline=discipline,
            bound_by=user,
            is_override=override,
            reason=(reason or "").strip(),
        )
    except IntegrityError as exc:
        raise BindingError("Дисциплина уже привязана к этой лаборатории.") from exc


@transaction.atomic
def unbind(user: User, laboratory: Laboratory, discipline: Discipline) -> None:
    if not staff_can_manage_catalog(user):
        raise BindingError("Отвязка доступна только заведующему лабораторией или сисадмину.")
    deleted, _ = LabDisciplineBinding.objects.filter(
        laboratory=laboratory, discipline=discipline
    ).delete()
    if not deleted:
        raise BindingError("Привязка не найдена.")
