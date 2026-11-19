"""Видимость данных: студенческий скоуп (учебный план группы) и каталог сотрудников.

Студентческий скоуп выводится через учебный план группы (GroupDisciplineLoad
с fallback на M2M) + исключения по ЛР (GroupLabWorkOverride).
Стафф-скоуп — по лаборатории сотрудника (SYS_ADMIN видит всё); УЦ дисциплины
выводится через laboratories → training_center (legacy training_centers срезан).
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q, QuerySet

from apps.academics.models import (
    Discipline,
    GroupDisciplineLoad,
    GroupLabWorkOverride,
    GroupLabWorkOverrideMode,
    LabWork,
    StudentGroup,
)
from apps.scheduling.models import Laboratory, TrainingCenter
from apps.users.models import User, UserRole


def _safe_profile(user: User):
    try:
        return user.profile
    except (AttributeError, ObjectDoesNotExist):
        return None


def published_disciplines_qs() -> QuerySet[Discipline]:
    return Discipline.objects.filter(
        is_published=True, is_archived=False, semester__is_active=True
    )


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
        is_archived=False,
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
            is_archived=False,
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


def group_discipline_ids(group: StudentGroup, semester_id: int | None = None) -> set[int]:
    """Дисциплины учебного плана группы; fallback — M2M, если нагрузки ещё нет."""
    loads = GroupDisciplineLoad.objects.filter(group=group, is_active=True)
    if semester_id is not None:
        loads = loads.filter(semester_id=semester_id)
    else:
        loads = loads.filter(semester__is_active=True)
    ids = set(loads.values_list("discipline_id", flat=True))
    if not ids:
        ids = set(group.disciplines.values_list("pk", flat=True))
    return ids


def group_can_take_lab_work(
    group: StudentGroup, lab_work: LabWork, semester_id: int | None = None
) -> bool:
    """Группа изучает ЛР, если она есть в её плане или хотя бы её дисциплина."""
    if group.lab_works.filter(pk=lab_work.pk).exists():
        return True
    discipline_ids = group_discipline_ids(group, semester_id)
    return bool(discipline_ids) and lab_work.disciplines.filter(
        pk__in=discipline_ids
    ).exists()


def laboratory_study_groups_qs(laboratory, semester_id: int | None = None) -> QuerySet[StudentGroup]:
    """Группы, чей учебный план пересекается с ЛР лаборатории."""
    lab_work_ids = laboratory.lab_works.values_list("pk", flat=True)
    discipline_ids = Discipline.objects.filter(
        lab_works__in=lab_work_ids
    ).values_list("pk", flat=True)
    load_filter = Q(
        discipline_loads__discipline_id__in=discipline_ids,
        discipline_loads__is_active=True,
    )
    if semester_id is not None:
        load_filter &= Q(discipline_loads__semester_id=semester_id)
    else:
        load_filter &= Q(discipline_loads__semester__is_active=True)
    return (
        StudentGroup.objects.filter(
            load_filter
            | Q(disciplines__in=discipline_ids)
            | Q(lab_works__in=lab_work_ids)
        )
        .distinct()
        .order_by("name")
    )


def teacher_department_id(user: User) -> int | None:
    profile = _safe_profile(user)
    return profile.department_id if profile else None


def teacher_lab_works_qs(user: User) -> QuerySet[LabWork]:
    """Опубликованные ЛР активного семестра по кафедре преподавателя."""
    department_id = teacher_department_id(user)
    if not department_id:
        return LabWork.objects.none()
    return LabWork.objects.filter(
        is_published=True,
        is_archived=False,
        disciplines__semester__is_active=True,
        disciplines__department_id=department_id,
    ).distinct()


def laboratory_teacher_users_qs(laboratory) -> QuerySet[User]:
    """Преподаватели кафедр, к которым относятся дисциплины лаборатории."""
    department_ids = Discipline.objects.filter(
        lab_works__laboratories=laboratory
    ).values_list("department_id", flat=True)
    return User.objects.filter(
        role=UserRole.TEACHER,
        profile__department_id__in=department_ids,
    ).select_related("profile")


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


def resolve_staff_training_center(user: User) -> TrainingCenter | None:
    """УЦ сотрудника: УЦ лаборатории профиля, иначе УЦ профиля."""
    profile = _safe_profile(user)
    if not profile:
        return None
    if profile.laboratory_id:
        return profile.laboratory.training_center
    return profile.training_center


def laboratory_people_qs(laboratory: Laboratory) -> QuerySet[User]:
    """Люди лаборатории для селекта дежурного: сотрудники лабы и преподаватели.

    Управление «людьми» в портале срезано — персонал приходит через
    seed_demo/CSV/админку с laboratories в профиле.
    """
    return User.objects.filter(
        profile__laboratory=laboratory,
        role__in=[UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.TEACHER],
    ).order_by("last_name", "first_name")


def student_support_training_centers_qs(user: User) -> QuerySet[TrainingCenter]:
    """УЦ, куда студент может написать в поддержку: через свои дисциплины и ЛР."""
    group = resolve_student_group(user)
    if not group:
        return TrainingCenter.objects.none()
    discipline_ids = student_disciplines_qs(user).values_list("pk", flat=True)
    lab_work_ids = student_lab_works_qs(user).values_list("pk", flat=True)
    return TrainingCenter.objects.filter(
        Q(laboratories__disciplines__in=discipline_ids) | Q(lab_works__in=lab_work_ids)
    ).distinct()


# --- Каталог сотрудников --------------------------------------------------------


def _staff_catalog_qs(user, published_qs, all_qs, laboratory):
    """Общий каркас скоупа каталога: сисадмин — всё; по лаборатории; иначе по УЦ."""
    if user.role == UserRole.SYS_ADMIN:
        return published_qs
    if laboratory:
        return published_qs.filter(laboratories=laboratory)
    tc = resolve_staff_training_center(user)
    if not tc:
        return all_qs.none()
    return published_qs.filter(laboratories__training_center=tc)


def staff_disciplines_qs(user: User) -> QuerySet[Discipline]:
    """Опубликованные дисциплины активного семестра в зоне доступа сотрудника."""
    lab = resolve_staff_laboratory(user)
    return _staff_catalog_qs(
        user,
        published_disciplines_qs().select_related("semester", "department"),
        Discipline.objects,
        lab,
    )


def lab_head_disciplines_qs(user: User) -> QuerySet[Discipline]:
    """Все дисциплины лаборатории для панели завлаба (включая неопубликованные)."""
    return _staff_catalog_qs(
        user,
        Discipline.objects.select_related("semester", "department"),
        Discipline.objects,
        resolve_staff_laboratory(user),
    ).order_by("title")


def staff_lab_works_qs(user: User, discipline_id: int | None = None) -> QuerySet[LabWork]:
    if user.role == UserRole.TEACHER:
        qs = teacher_lab_works_qs(user)
        if discipline_id is not None:
            qs = qs.filter(disciplines=discipline_id)
        return qs
    qs = LabWork.objects.filter(
        is_published=True, is_archived=False, disciplines__semester__is_active=True
    ).distinct()
    if discipline_id is not None:
        qs = qs.filter(disciplines=discipline_id)
    return _staff_catalog_qs(user, qs, LabWork.objects, resolve_staff_laboratory(user))


def lab_head_lab_works_qs(user: User) -> QuerySet[LabWork]:
    """Все ЛР лаборатории для панели завлаба (включая неопубликованные)."""
    return _staff_catalog_qs(
        user,
        LabWork.objects.prefetch_related("disciplines", "disciplines__department"),
        LabWork.objects,
        resolve_staff_laboratory(user),
    ).order_by("number", "title")


def staff_students_qs(user: User) -> QuerySet[User]:
    """Студенты, чей учебный план пересекается с дисциплинами лаборатории,
    либо имеющие записи в зоне доступа сотрудника."""
    from apps.bookings.scope import staff_bookings_qs

    qs = User.objects.filter(role=UserRole.STUDENT).select_related(
        "profile", "profile__student_group"
    )
    if user.role == UserRole.SYS_ADMIN:
        return qs.order_by("last_name", "first_name", "email")

    discipline_ids = lab_head_disciplines_qs(user).values_list("pk", flat=True)
    booking_student_ids = staff_bookings_qs(user).values_list("student_id", flat=True)
    if not discipline_ids and not booking_student_ids:
        return User.objects.none()

    filters = Q(pk__in=booking_student_ids)
    if discipline_ids:
        loaded_group_ids = GroupDisciplineLoad.objects.filter(
            semester__is_active=True, is_active=True, discipline_id__in=discipline_ids
        ).values_list("group_id", flat=True)
        fallback_group_ids = StudentGroup.objects.filter(
            disciplines__in=discipline_ids
        ).values_list("pk", flat=True)
        filters |= (
            Q(profile__student_group_id__in=loaded_group_ids)
            | Q(profile__student_group_id__in=fallback_group_ids)
            | Q(profile__student_group__lab_works__disciplines__in=discipline_ids)
        )
    return qs.filter(filters).distinct().order_by("last_name", "first_name", "email")


def staff_writable_lab_works_qs(user: User, discipline_id: int | None = None) -> QuerySet[LabWork]:
    """ЛР, в слоты которых сотрудник может писать вручную.

    LAB_ADMIN — только ЛР, у которых есть слот в закреплённой за ним комнате;
    LAB_HEAD — вся лаборатория; TEACHER — ничего.
    """
    qs = staff_lab_works_qs(user, discipline_id=discipline_id)
    if user.role == UserRole.LAB_ADMIN:
        return qs.filter(sessions__room__default_lab_staff=user).distinct()
    if user.role == UserRole.TEACHER:
        return qs.none()
    return qs


def staff_manual_lab_work_items(user: User, student: User) -> QuerySet[LabWork]:
    """ЛР для ручной записи: учебный план группы студента ∩ зона сотрудника."""
    plan_ids = set(student_lab_works_qs(student).values_list("pk", flat=True))
    if not plan_ids:
        return LabWork.objects.none()
    return staff_writable_lab_works_qs(user).filter(pk__in=plan_ids)


def user_can_view_methodics(user: User, lab_work: LabWork) -> bool:
    """Видимость методичек: только авторизованным и только по своей зоне.

    Студент — ЛР из своего учебного плана; завлаб — работы своей лаборатории
    (включая неопубликованные); сотрудник и преподаватель — по обычному
    стафф-скоупу; сисадмин — всё.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.role == UserRole.SYS_ADMIN:
        return True
    if user.role == UserRole.STUDENT:
        return student_can_access_lab_work(user, lab_work.pk)
    if user.role == UserRole.LAB_HEAD:
        return lab_head_lab_works_qs(user).filter(pk=lab_work.pk).exists()
    return staff_lab_works_qs(user).filter(pk=lab_work.pk).exists()
