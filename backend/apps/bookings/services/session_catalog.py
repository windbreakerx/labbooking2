"""Векторизованный каталог слотов: константное число запросов вместо ~5×S.

Студенческий каталог (окно записи, праздники, места, whitelist расписания,
занятость студента) и окно ручной записи сотрудников собираются здесь одним
блоком запросов. Места считаются общей математикой (seat_capacity), поэтому
каталог показывает ровно то, что проверяют пути брони.
"""

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.academics.models import LabWork, LabWorkBookingRelease
from apps.academics.scope import resolve_student_group
from apps.bookings.models import Booking, BookingStatus
from apps.bookings.services.seat_capacity import seats_available
from apps.bookings.services.session_availability import (
    booking_date_window,
    booking_window_configs,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_pair_time_for_booking,
    is_session_in_manual_booking_window,
    manual_booking_max_date,
)
from apps.scheduling.models import (
    Holiday,
    LabSession,
    LabSessionBookingMode,
    LabSessionStatus,
    LabStand,
)
from apps.users.models import User


@dataclass
class SessionSlot:
    """Слот каталога: сессия + места (booked_count — для ручной записи)."""

    session: LabSession
    available_seats: int
    booked_count: int


def student_bookable_slots(
    lab_work_id: int | None = None,
    *,
    student: User | None = None,
    now: datetime | None = None,
) -> list[SessionSlot]:
    """Слоты, доступные студенту для записи: окно + праздники + места + whitelist + занятость."""
    moment = now or timezone.now()
    min_date, max_date = booking_date_window(moment)
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))
    qs = (
        LabSession.objects.filter(
            status=LabSessionStatus.OPEN,
            booking_mode=LabSessionBookingMode.INDIVIDUAL,
            starts_at__gt=moment,
            starts_at__date__gte=min_date,
            starts_at__date__lte=max_date,
            room__is_blocked=False,
        )
        .select_related("lab_work", "room", "room__training_center", "room__laboratory")
        .order_by("starts_at")
    )
    if lab_work_id:
        qs = qs.filter(lab_work_id=lab_work_id)
    if holiday_dates:
        qs = qs.exclude(starts_at__date__in=holiday_dates)
    sessions = list(qs)
    if not sessions:
        return []
    configs = booking_window_configs({session.room.laboratory_id for session in sessions})
    counters = _seat_counters(sessions)
    busy_intervals = _student_busy_intervals(student)
    open_releases = _open_release_keys(sessions, moment)
    allowed_group_ids = _session_allowed_group_ids(sessions)
    student_group = resolve_student_group(student)
    slots: list[SessionSlot] = []
    for session in sessions:
        config = configs[session.room.laboratory_id]
        local_date = timezone.localtime(session.starts_at).date()
        if not is_pair_time_for_booking(session.starts_at):
            continue
        if not is_day_open_for_booking(local_date, moment, config=config):
            continue
        if not is_before_restriction_deadline(session.starts_at, moment, config=config):
            continue
        if (session.lab_work_id, session.semester_id) not in open_releases:
            continue
        session_allowed = allowed_group_ids.get(session.pk, set())
        if session_allowed and (student_group is None or student_group.pk not in session_allowed):
            continue
        if _overlaps_any_interval(session, busy_intervals):
            continue
        counter = counters[session.pk]
        available = _session_available_seats(session, counter)
        if available <= 0:
            continue
        slots.append(SessionSlot(session, available, counter["session_booked"]))
    return slots


def staff_manual_slots(
    lab_work_id: int, *, now: datetime | None = None, room_ids=None
) -> list[SessionSlot]:
    """Слоты для ручной записи: с текущего момента до N рабочих недель.

    Фильтра по местам нет — лимиты ручной записи дают предупреждения,
    а не запрет (BookingService._validate_manual_booking_rules). ``room_ids``
    ограничивает выборку комнатами, в которые сотруднику разрешено писать.
    """
    moment = now or timezone.now()
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))
    max_date = manual_booking_max_date(moment, holiday_dates=holiday_dates)
    qs = (
        LabSession.objects.filter(
            status=LabSessionStatus.OPEN,
            ends_at__gt=moment,
            starts_at__date__lte=max_date,
            lab_work_id=lab_work_id,
            room__is_blocked=False,
        )
        .select_related("lab_work", "room", "room__training_center")
        .order_by("starts_at")
    )
    if room_ids is not None:
        qs = qs.filter(room_id__in=room_ids)
    sessions = list(qs)
    counters = _seat_counters(sessions)
    slots: list[SessionSlot] = []
    for session in sessions:
        if not is_session_in_manual_booking_window(
            session, moment, max_date=max_date, holiday_dates=holiday_dates
        ):
            continue
        counter = counters[session.pk]
        available = _session_available_seats(session, counter)
        slots.append(SessionSlot(session, available, counter["session_booked"]))
    return slots


def _seat_counters(sessions: list[LabSession]) -> dict[int, dict[str, int | bool]]:
    """Счётчики мест всех слотов одним блоком запросов.

    Пересечения считаются в Python; семантика совпадает со скалярными
    session_seat_counts: записи в слот, чужие записи в аудитории,
    занятость стенда этой ЛР.
    """
    if not sessions:
        return {}
    min_starts = min(session.starts_at for session in sessions)
    max_ends = max(session.ends_at for session in sessions)
    counters = {
        session.pk: {"session_booked": 0, "room_other": 0, "stand_blocked": False}
        for session in sessions
    }
    rows = Booking.objects.filter(
        current_status=BookingStatus.BOOKED,
        lab_session__starts_at__lt=max_ends,
        lab_session__ends_at__gt=min_starts,
    ).values(
        "lab_session_id",
        "lab_session__room_id",
        "lab_session__starts_at",
        "lab_session__ends_at",
    )
    for row in rows:
        for session in sessions:
            if row["lab_session_id"] == session.pk:
                counters[session.pk]["session_booked"] += 1
                continue
            if (
                row["lab_session__starts_at"] < session.ends_at
                and row["lab_session__ends_at"] > session.starts_at
                and row["lab_session__room_id"] == session.room_id
            ):
                counters[session.pk]["room_other"] += 1
    _mark_stand_conflicts(sessions, counters, min_starts, max_ends)
    return counters


def _mark_stand_conflicts(
    sessions: list[LabSession],
    counters: dict[int, dict[str, int | bool]],
    min_starts,
    max_ends,
) -> None:
    """Проставить stand_blocked: чужая ЛР на том же стенде или стенды в комнате кончились."""
    lab_work_ids = {session.lab_work_id for session in sessions}
    through = LabWork.stands.through
    stand_rows = list(
        through.objects.filter(labwork_id__in=lab_work_ids).values_list(
            "labwork_id", "labstand_id"
        )
    )
    if not stand_rows:
        return
    stand_rooms = dict(
        LabStand.objects.filter(pk__in={row[1] for row in stand_rows}).values_list("id", "room_id")
    )
    stands_by_lab: dict[int, set[int]] = {}
    for lab_work_id, stand_id in stand_rows:
        stands_by_lab.setdefault(lab_work_id, set()).add(stand_id)

    overlapping = list(
        LabSession.objects.filter(starts_at__lt=max_ends, ends_at__gt=min_starts)
        .exclude(status=LabSessionStatus.CANCELLED)
        .values("id", "lab_work_id", "room_id", "starts_at", "ends_at")
    )
    for session in sessions:
        stands = stands_by_lab.get(session.lab_work_id, set())
        if not stands:
            continue
        stands_in_room = sum(1 for stand_id in stands if stand_rooms.get(stand_id) == session.room_id)
        if stands_in_room == 0:
            counters[session.pk]["stand_blocked"] = True
            continue
        parallel = 0
        for other in overlapping:
            if other["id"] == session.pk or not (
                other["starts_at"] < session.ends_at and other["ends_at"] > session.starts_at
            ):
                continue
            if other["lab_work_id"] == session.lab_work_id:
                if other["room_id"] == session.room_id:
                    parallel += 1
                continue
            if stands & stands_by_lab.get(other["lab_work_id"], set()):
                counters[session.pk]["stand_blocked"] = True
                break
        if parallel >= stands_in_room:
            counters[session.pk]["stand_blocked"] = True


def _session_available_seats(session: LabSession, counter: dict) -> int:
    return seats_available(
        session_capacity=session.capacity,
        session_booked=counter["session_booked"],
        room_capacity=session.room.capacity,
        room_other_booked=counter["room_other"],
        stand_blocked=counter["stand_blocked"],
    )


def _student_busy_intervals(student: User | None) -> list[tuple]:
    if student is None:
        return []
    return list(
        student.bookings.filter(current_status=BookingStatus.BOOKED).values_list(
            "lab_session__starts_at", "lab_session__ends_at"
        )
    )


def _overlaps_any_interval(session: LabSession, intervals: list[tuple]) -> bool:
    return any(start < session.ends_at and end > session.starts_at for start, end in intervals)


def _session_allowed_group_ids(sessions: list[LabSession]) -> dict[int, set[int]]:
    """Разрешённые группы для каждой сессии одним запросом (пусто = все)."""
    if not sessions:
        return {}
    allowed: dict[int, set[int]] = {session.pk: set() for session in sessions}
    through = LabSession.allowed_student_groups.through
    for session_id, group_id in through.objects.filter(
        labsession_id__in=allowed.keys()
    ).values_list("labsession_id", "studentgroup_id"):
        allowed[session_id].add(group_id)
    return allowed


def _open_release_keys(sessions: list[LabSession], moment: datetime) -> set[tuple[int, int]]:
    """Ключи (lab_work_id, semester_id) с открытой записью на момент ``moment``."""
    if not sessions:
        return set()
    keys = {(session.lab_work_id, session.semester_id) for session in sessions}
    return {
        (row["lab_work_id"], row["semester_id"])
        for row in LabWorkBookingRelease.objects.filter(
            booking_opens_at__lte=moment,
        )
        .values("lab_work_id", "semester_id")
        if (row["lab_work_id"], row["semester_id"]) in keys
    }


# --- Отображение состояния релиза и групповых слотов ----------------------------


def booking_release_status(lab_work_id: int, semester_id: int, now=None) -> dict:
    """Состояние открытия записи на ЛР в семестре для отображения в каталоге."""
    release = LabWorkBookingRelease.objects.filter(
        lab_work_id=lab_work_id, semester_id=semester_id
    ).first()
    if release is None or release.booking_opens_at is None:
        return {"state": "closed", "opens_at": None, "label": "Запись закрыта"}
    moment = now or timezone.now()
    if release.booking_opens_at <= moment:
        return {"state": "open", "opens_at": None, "label": "Запись открыта"}
    local = timezone.localtime(release.booking_opens_at)
    return {
        "state": "future",
        "opens_at": release.booking_opens_at,
        "label": f"Запись откроется {local:%d.%m.%Y в %H:%M}",
    }


def student_group_sessions(lab_work_id: int, student, now=None) -> list[LabSession]:
    """Групповые слоты ЛР, видимые студенту (его группа-оккупант)."""
    group = resolve_student_group(student)
    if group is None:
        return []
    moment = now or timezone.now()
    return list(
        LabSession.objects.filter(
            lab_work_id=lab_work_id,
            booking_mode=LabSessionBookingMode.GROUP,
            occupant_group=group,
            status=LabSessionStatus.OPEN,
            starts_at__gt=moment,
        )
        .select_related("room", "room__training_center")
        .order_by("starts_at")
    )
