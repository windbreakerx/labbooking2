"""Видимость данных для студента: что он может видеть и на что записываться.

Студентческий скоуп выводится через учебный план группы (GroupDisciplineLoad
с fallback на M2M) + исключения по ЛР (GroupLabWorkOverride).
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import QuerySet

from apps.academics.models import (
    Discipline,
    GroupDisciplineLoad,
    GroupLabWorkOverride,
    GroupLabWorkOverrideMode,
    LabWork,
    StudentGroup,
)
from apps.scheduling.models import Laboratory
from apps.users.models import User, UserRole


def _safe_profile(user: User):
    try:
        return user.profile
    except (AttributeError, ObjectDoesNotExist):
        return None


def published_disciplines_qs() -> QuerySet[Discipline]:
    return Discipline.objects.filter(is_published=True, semester__is_active=True)


def resolve_student_group(user: User) -> StudentGroup | None:
    profile = _safe_profile(user)
    if not profile:
        return None
    if profile.student_group_id:
        return profile.student_group
    if profile.group_name:
        return StudentGroup.objects.filter(name=profile.group_name).first()
    return None


def _active_group_discipline_ids(group: StudentGroup) -> list[int]:
    load_ids = list(
        GroupDisciplineLoad.objects.filter(
            group=group,
            semester__is_active=True,
            is_active=True,
        ).values_list("discipline_id", flat=True).distinct()
    )
    if load_ids:
        return load_ids
    return list(group.disciplines.values_list("pk", flat=True))


def _active_lab_work_override_ids(group: StudentGroup, *, mode: str) -> list[int]:
    return list(
        GroupLabWorkOverride.objects.filter(
            group=group,
            semester__is_active=True,
            is_active=True,
            mode=mode,
        ).values_list("lab_work_id", flat=True).distinct()
    )


def student_disciplines_qs(user: User) -> QuerySet[Discipline]:
    qs = published_disciplines_qs()
    group = resolve_student_group(user)
    if not group:
        return qs.none()
    discipline_ids = _active_group_discipline_ids(group)
    if not discipline_ids:
        return qs.none()
    return qs.filter(pk__in=discipline_ids)


def student_lab_works_qs(user: User, discipline_id: int | None = None) -> QuerySet[LabWork]:
    group = resolve_student_group(user)
    if not group:
        return LabWork.objects.none()

    discipline_ids = _active_group_discipline_ids(group)
    if not discipline_ids:
        return LabWork.objects.none()
    if discipline_id is not None and discipline_id not in discipline_ids:
        return LabWork.objects.none()

    qs = LabWork.objects.filter(
        is_published=True,
        disciplines__semester__is_active=True,
        disciplines__in=discipline_ids,
    ).distinct()
    if discipline_id is not None:
        qs = qs.filter(disciplines=discipline_id)

    hidden_ids = _active_lab_work_override_ids(group, mode=GroupLabWorkOverrideMode.HIDDEN)
    if hidden_ids:
        qs = qs.exclude(pk__in=hidden_ids)

    open_ids = _active_lab_work_override_ids(group, mode=GroupLabWorkOverrideMode.OPEN)
    if open_ids:
        open_qs = LabWork.objects.filter(
            pk__in=open_ids,
            is_published=True,
            disciplines__semester__is_active=True,
        ).distinct()
        if discipline_id is not None:
            open_qs = open_qs.filter(disciplines=discipline_id)
        qs = (qs | open_qs).distinct()

    return qs


def student_can_access_discipline(user: User, discipline_id: int) -> bool:
    if user.role != UserRole.STUDENT:
        return True
    return student_disciplines_qs(user).filter(pk=discipline_id).exists()


def student_can_access_lab_work(user: User, lab_work_id: int) -> bool:
    if user.role != UserRole.STUDENT:
        return True
    return student_lab_works_qs(user).filter(pk=lab_work_id).exists()


def resolve_staff_laboratory(user: User) -> Laboratory | None:
    """Лаборатория сотрудника: из профиля, иначе первая лаборатория его УЦ."""
    profile = _safe_profile(user)
    if not profile:
        return None
    if profile.laboratory_id:
        return profile.laboratory
    if not profile.training_center:
        return None
    return profile.training_center.laboratories.order_by("name").first()
