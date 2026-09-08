"""Векторизованный каталог слотов: константное число запросов вместо ~5×S.

Студенческий каталог (окно записи, праздники, места, whitelist расписания,
занятость студента) и окно ручной записи сотрудников собираются здесь одним
блоком запросов. Места считаются общей математикой (seat_capacity), поэтому
каталог показывает ровно то, что проверяют пути брони.
"""

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus
from apps.bookings.services.seat_capacity import seats_available
from apps.bookings.services.session_availability import (
    ScheduleWhitelistIndex,
    booking_date_window,
    booking_window_configs,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_pair_time_for_booking,
    is_session_in_manual_booking_window,
    manual_booking_max_date,
)
from apps.scheduling.models import Holiday, LabSession, LabSessionStatus
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
    whitelist = ScheduleWhitelistIndex(sessions)
    busy_intervals = _student_busy_intervals(student)
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
        if _overlaps_any_interval(session, busy_intervals):
            continue
        if not whitelist.allows(session, student=student):
            continue
        counter = counters[session.pk]
        available = _session_available_seats(session, counter)
        if available <= 0:
            continue
        slots.append(SessionSlot(session, available, counter["session_booked"]))
    return slots


def staff_manual_slots(lab_work_id: int, *, now: datetime | None = None) -> list[SessionSlot]:
    """Слоты для ручной записи: с текущего момента до N рабочих недель.

    Фильтра по местам нет — лимиты ручной записи дают предупреждения,
    а не запрет (BookingService._validate_manual_booking_rules).
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
    """Счётчики мест всех слотов одним запросом по записям BOOKED.

    Пересечения считаются в Python; семантика совпадает со скалярными
    session_seat_counts: чужие слоты той же ЛР / той же аудитории,
    первичный стенд, занятый другой ЛР.
    """
    if not sessions:
        return {}
    min_starts = min(session.starts_at for session in sessions)
    max_ends = max(session.ends_at for session in sessions)
    counters = {
        session.pk: {"session_booked": 0, "lab_other": 0, "room_other": 0, "stand_blocked": False}
        for session in sessions
    }
    rows = Booking.objects.filter(
        current_status=BookingStatus.BOOKED,
        lab_session__starts_at__lt=max_ends,
        lab_session__ends_at__gt=min_starts,
    ).values(
        "lab_session_id",
        "lab_session__lab_work_id",
        "lab_session__room_id",
        "lab_session__starts_at",
        "lab_session__ends_at",
        "lab_session__lab_work__primary_stand_id",
    )
    for row in rows:
        for session in sessions:
            _accumulate_seat_counters(session, counters[session.pk], row)
    return counters


def _accumulate_seat_counters(session: LabSession, counter: dict, row: dict) -> None:
    if row["lab_session_id"] == session.pk:
        counter["session_booked"] += 1
        return
    if not (
        row["lab_session__starts_at"] < session.ends_at
        and row["lab_session__ends_at"] > session.starts_at
    ):
        return
    if row["lab_session__lab_work_id"] == session.lab_work_id:
        counter["lab_other"] += 1
    if row["lab_session__room_id"] == session.room_id:
        counter["room_other"] += 1
    stand_id = session.lab_work.primary_stand_id
    if (
        stand_id
        and row["lab_session__lab_work__primary_stand_id"] == stand_id
        and row["lab_session__lab_work_id"] != session.lab_work_id
    ):
        counter["stand_blocked"] = True


def _session_available_seats(session: LabSession, counter: dict) -> int:
    return seats_available(
        session_capacity=session.capacity,
        session_booked=counter["session_booked"],
        lab_work_capacity=session.lab_work.capacity,
        lab_other_booked=counter["lab_other"],
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
