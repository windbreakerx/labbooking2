"""Фильтры/сортировка таблицы броней и поиск студентов для стафф-UI.

Порт v1 (services/booking.py): видимость записей — staff_bookings_qs
(bookings/scope.py), здесь только обработка ?-параметров. Бизнес-правила
брони эти функции не трогают.
"""

from django.db.models import Q

from apps.users.models import User, UserRole

BOOKING_SORT_FIELDS: dict[str, tuple[list[str], str]] = {
    "student": (["student__last_name", "student__first_name", "pk"], "asc"),
    "group": (
        ["student__profile__group_name", "student__profile__student_group__name", "pk"],
        "asc",
    ),
    "discipline": (["discipline__title", "pk"], "asc"),
    "lab_work": (["lab_work__number", "lab_work__title", "pk"], "asc"),
    "date": (["scheduled_at", "pk"], "desc"),
    "registration": (
        ["registration_type", "registered_by__last_name", "registered_by__first_name", "pk"],
        "asc",
    ),
    "status": (["current_status", "pk"], "asc"),
    "training_center": (["room__training_center__number", "pk"], "asc"),
    "room": (["room__number", "pk"], "asc"),
}


def order_bookings_queryset(qs, params):
    sort_key = params.get("sort")
    if not sort_key or sort_key not in BOOKING_SORT_FIELDS:
        return qs.order_by("-scheduled_at")
    fields, default_dir = BOOKING_SORT_FIELDS[sort_key]
    direction = params.get("dir", default_dir)
    if direction not in {"asc", "desc"}:
        direction = default_dir
    if direction == "desc":
        return qs.order_by(*(f"-{field}" for field in fields))
    return qs.order_by(*fields)


def filter_staff_bookings(qs, params):
    if status_val := params.get("status"):
        qs = qs.filter(current_status=status_val)
    if group_q := params.get("group"):
        qs = qs.filter(
            Q(student__profile__student_group__name__icontains=group_q)
            | Q(student__profile__group_name__icontains=group_q)
        )
    if discipline_id := params.get("discipline"):
        qs = qs.filter(discipline_id=discipline_id)
    if room_q := params.get("room"):
        qs = qs.filter(room__number__icontains=room_q)
    if date_from := params.get("date_from"):
        qs = qs.filter(scheduled_at__date__gte=date_from)
    if date_to := params.get("date_to"):
        qs = qs.filter(scheduled_at__date__lte=date_to)
    if student_q := params.get("student"):
        qs = qs.filter(_student_search_q(student_q, prefix="student__"))
    return qs


def _student_search_q(query: str, *, prefix: str = ""):
    query = query.strip()
    parts = query.split()
    if len(parts) >= 2:
        return (
            Q(**{f"{prefix}last_name__icontains": parts[0], f"{prefix}first_name__icontains": parts[-1]})
            | Q(**{f"{prefix}email__icontains": query})
            | Q(**{f"{prefix}profile__group_name__icontains": query})
            | Q(**{f"{prefix}profile__student_group__name__icontains": query})
        )
    return (
        Q(**{f"{prefix}email__icontains": query})
        | Q(**{f"{prefix}last_name__icontains": query})
        | Q(**{f"{prefix}first_name__icontains": query})
        | Q(**{f"{prefix}profile__group_name__icontains": query})
        | Q(**{f"{prefix}profile__student_group__name__icontains": query})
    )


def search_students_for_staff(query: str, limit: int = 15):
    """Поиск студентов для ручной записи (глобальный; план группы проверяет сервис)."""
    query = (query or "").strip()
    if len(query) < 2:
        return User.objects.none()
    return (
        User.objects.filter(role=UserRole.STUDENT)
        .select_related("profile", "profile__student_group")
        .filter(_student_search_q(query))
        .order_by("last_name", "first_name", "email")[:limit]
    )
