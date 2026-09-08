"""Доступность слотов: окна записи, университетские пары, лимиты мест,
whitelist расписания, авто-«Посетил».

Порт из v1 (971 строк) без каскадных опций визарда — те вернутся в День 6
вместе со студенческим UI.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from apps.scheduling.models import (
    AutoVisitedMode,
    Holiday,
    LaboratoryBookingSettings,
    LabSession,
    LabSessionStatus,
    ScheduleDutyRole,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
    WeekParity,
)

UNIVERSITY_PAIR_SLOTS = [
    (1, time(8, 50), time(10, 20)),
    (2, time(10, 35), time(12, 5)),
    (3, time(12, 35), time(14, 5)),
    (4, time(14, 15), time(15, 45)),
    (5, time(15, 55), time(17, 20)),
    (6, time(17, 30), time(19, 0)),
]
BOOKING_START_GRID_MINUTES = 15


# --- Автопроставление VISITED -------------------------------------------------


def auto_visited_deadline(session_starts_at: datetime, mode: str) -> datetime | None:
    if mode == AutoVisitedMode.MANUAL:
        return None
    local_start = timezone.localtime(session_starts_at)
    tz = timezone.get_current_timezone()
    if mode == AutoVisitedMode.AT_22_00:
        return timezone.make_aware(datetime.combine(local_start.date(), time(22, 0)), tz)
    if mode == AutoVisitedMode.AT_09_00_NEXT:
        return timezone.make_aware(
            datetime.combine(local_start.date() + timedelta(days=1), time(9, 0)), tz
        )
    return None


def is_ready_for_auto_visited(
    session: LabSession,
    now: datetime | None = None,
    *,
    auto_visited_mode: str | None = None,
) -> bool:
    mode = auto_visited_mode or AutoVisitedMode.MANUAL
    if mode == AutoVisitedMode.MANUAL:
        return False
    moment = timezone.localtime(now or timezone.now())
    if session.ends_at > moment:
        return False
    deadline = auto_visited_deadline(session.starts_at, mode)
    return deadline is not None and moment >= deadline


# --- Окно записи ---------------------------------------------------------------


def booking_window_config(laboratory_id: int | None = None) -> dict:
    """Глобальные настройки окна записи с переопределениями конкретной лаборатории."""
    config = {
        "horizon_days": settings.BOOKING_HORIZON_DAYS,
        "cancel_hours": settings.BOOKING_CANCEL_HOURS,
        "restriction_base_time": time(9, 0),
        "restriction_hours_before_base": None,
    }
    if not laboratory_id:
        return config
    lab_settings = LaboratoryBookingSettings.objects.filter(laboratory_id=laboratory_id).first()
    if not lab_settings:
        return config
    if lab_settings.booking_horizon_days is not None:
        config["horizon_days"] = lab_settings.booking_horizon_days
    if lab_settings.booking_cancel_hours is not None:
        config["cancel_hours"] = lab_settings.booking_cancel_hours
    config["restriction_base_time"] = lab_settings.restriction_base_time
    config["restriction_hours_before_base"] = lab_settings.restriction_hours_before_base
    return config


def booking_date_window(now: datetime | None = None, *, laboratory_id: int | None = None) -> tuple:
    """Рабочее окно дат записи: всегда с завтрашнего дня и до горизонта N дней.

    Закрытие записи на следующий день регулируется is_before_restriction_deadline.
    """
    local_now = timezone.localtime(now or timezone.now())
    config = booking_window_config(laboratory_id)
    min_date = local_now.date() + timedelta(days=1)
    max_date = local_now.date() + timedelta(days=config["horizon_days"])
    return min_date, max_date


def is_day_open_for_booking(
    session_date, now: datetime | None = None, *, laboratory_id: int | None = None
) -> bool:
    min_date, max_date = booking_date_window(now, laboratory_id=laboratory_id)
    return min_date <= session_date <= max_date


def is_before_restriction_deadline(
    session_starts_at: datetime,
    now: datetime | None = None,
    *,
    laboratory_id: int | None = None,
) -> bool:
    config = booking_window_config(laboratory_id)
    restriction_hours = config["restriction_hours_before_base"]
    if restriction_hours is None:
        return True
    local_now = timezone.localtime(now or timezone.now())
    local_start = timezone.localtime(session_starts_at)
    cutoff_dt = timezone.make_aware(
        datetime.combine(local_start.date(), config["restriction_base_time"]),
        timezone.get_current_timezone(),
    ) - timedelta(hours=restriction_hours)
    return local_now <= cutoff_dt


def manual_booking_max_date(now: datetime | None = None):
    """Последний календарный день в пределах N рабочих недель от сегодня."""
    local_now = timezone.localtime(now or timezone.now())
    target_working_days = settings.MANUAL_BOOKING_WORKING_WEEKS * 5
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))

    working_days = 0
    cursor = local_now.date()
    while working_days < target_working_days:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in holiday_dates:
            working_days += 1
    return cursor


# --- Университетские пары ------------------------------------------------------


def _minutes_between(start: time, end: time) -> int:
    return end.hour * 60 + end.minute - (start.hour * 60 + start.minute)


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _pair_slot_for_time(moment: time) -> tuple[int, time, time] | None:
    minute_value = moment.hour * 60 + moment.minute
    for number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        if _time_to_minutes(pair_start) <= minute_value < _time_to_minutes(pair_end):
            return number, pair_start, pair_end
    return None


def is_pair_time_for_booking(session_dt: datetime) -> bool:
    """Начало слота лежит на 15-минутной сетке внутри университетской пары."""
    local_dt = timezone.localtime(session_dt)
    starts_at = local_dt.time().replace(second=0, microsecond=0)
    slot = _pair_slot_for_time(starts_at)
    if not slot:
        return False
    _, pair_start, _ = slot
    offset = _time_to_minutes(starts_at) - _time_to_minutes(pair_start)
    return offset >= 0 and offset % BOOKING_START_GRID_MINUTES == 0


def is_manual_session_time_allowed(session: LabSession, now: datetime | None = None) -> bool:
    """Ручная запись: будущий слот или текущая пара, если слот ещё не завершился."""
    moment = timezone.localtime(now or timezone.now())
    if session.ends_at <= moment:
        return False
    if session.starts_at > moment:
        return True
    local_start = timezone.localtime(session.starts_at)
    if local_start.date() != moment.date():
        return False
    pair_session = _pair_slot_for_time(local_start.time())
    pair_moment = _pair_slot_for_time(moment.time())
    return pair_session is not None and pair_moment is not None and pair_session[0] == pair_moment[0]


def is_session_in_manual_booking_window(
    session: LabSession,
    now: datetime | None = None,
    *,
    max_date=None,
    holiday_dates: set | None = None,
) -> bool:
    """Ручная запись: будущий слот в пределах N рабочих недель, пара, не праздник.

    ``max_date``/``holiday_dates`` считаются один раз вызывающим кодом
    (O(1) запросов на страницу вместо O(S)).
    """
    moment = now or timezone.now()
    session_local_date = timezone.localtime(session.starts_at).date()
    if max_date is None:
        max_date = manual_booking_max_date(moment)
    if session_local_date > max_date:
        return False
    if not is_manual_session_time_allowed(session, moment):
        return False
    if not is_pair_time_for_booking(session.starts_at):
        return False
    if holiday_dates is None:
        return not Holiday.objects.filter(date=session_local_date).exists()
    return session_local_date not in holiday_dates


# --- Whitelist расписания -------------------------------------------------------


def _session_parity(starts_at: datetime) -> str:
    week_is_odd = timezone.localtime(starts_at).date().isocalendar().week % 2 == 1
    return WeekParity.ODD if week_is_odd else WeekParity.EVEN


def _entry_lab_work_ids(entry: ScheduleEntry) -> set[int]:
    """ЛР слота: явные выборки по дисциплинам; пустая выборка = все ЛР дисциплины."""
    ids: set[int] = set()
    for selection in entry.discipline_selections.all():
        explicit = {lab_work.pk for lab_work in selection.lab_works.all()}
        if explicit:
            ids.update(explicit)
        else:
            ids.update(selection.discipline.lab_works.values_list("pk", flat=True))
    return ids


def _entry_matches_session(entry: ScheduleEntry, session: LabSession) -> bool:
    local_start = timezone.localtime(session.starts_at)
    local_end = timezone.localtime(session.ends_at)
    if entry.weekday != local_start.weekday():
        return False
    if entry.week_parity != WeekParity.BOTH and entry.week_parity != _session_parity(session.starts_at):
        return False
    if session.lab_work_id not in _entry_lab_work_ids(entry):
        return False
    entry_start_minutes = _time_to_minutes(entry.start_time)
    entry_end_minutes = entry_start_minutes + entry.duration_minutes
    if _time_to_minutes(local_start.time()) < entry_start_minutes:
        return False
    return _time_to_minutes(local_end.time()) <= entry_end_minutes


def _student_group_label(student) -> str:
    try:
        profile = student.profile
    except (AttributeError, ObjectDoesNotExist):
        return ""
    if profile.student_group_id:
        return (profile.student_group.name or "").strip()
    return (profile.group_name or "").strip()


def _filter_sessions_by_schedule_whitelist(qs: QuerySet[LabSession], *, student=None) -> QuerySet[LabSession]:
    """Слот видим студенту, если совпадает с активной записью расписания.

    Слоты TEACHER_DUTY с группой по нагрузке видны только этой группе.
    """
    sessions = list(qs)
    if not sessions:
        return qs.none()
    selections_qs = ScheduleEntryDisciplineSelection.objects.prefetch_related(
        "lab_works", "discipline__lab_works"
    )
    schedule_entries = (
        ScheduleEntry.objects.filter(
            is_active=True,
            room_id__in={session.room_id for session in sessions},
            semester_id__in={session.semester_id for session in sessions},
        )
        .prefetch_related(Prefetch("discipline_selections", queryset=selections_qs))
        .order_by("pk")
    )
    by_slot: dict[tuple[int, int, int], list[ScheduleEntry]] = {}
    for entry in schedule_entries:
        by_slot.setdefault((entry.room_id, entry.semester_id, entry.weekday), []).append(entry)

    group_label = _student_group_label(student) if student is not None else ""
    session_ids: list[int] = []
    for session in sessions:
        local_start = timezone.localtime(session.starts_at)
        candidates = by_slot.get((session.room_id, session.semester_id, local_start.weekday()), [])
        matched = [entry for entry in candidates if _entry_matches_session(entry, session)]
        if not matched:
            continue
        if student is not None:
            unrestricted = [
                entry
                for entry in matched
                if not (entry.duty_role == ScheduleDutyRole.TEACHER_DUTY and entry.load_group_label.strip())
            ]
            if unrestricted:
                session_ids.append(session.pk)
                continue
            restricted_groups = {
                entry.load_group_label.strip()
                for entry in matched
                if entry.duty_role == ScheduleDutyRole.TEACHER_DUTY
            }
            if group_label and group_label in restricted_groups:
                session_ids.append(session.pk)
            continue
        session_ids.append(session.pk)

    if not session_ids:
        return qs.none()
    return qs.filter(pk__in=session_ids).order_by("starts_at")


def session_matches_schedule_whitelist(session: LabSession, *, student=None) -> bool:
    return _filter_sessions_by_schedule_whitelist(
        LabSession.objects.filter(pk=session.pk),
        student=student,
    ).exists()


# --- Лимиты мест ---------------------------------------------------------------


def _overlapping_booked_count(session: LabSession, **lookup) -> int:
    from apps.bookings.models import Booking, BookingStatus

    return Booking.objects.filter(
        current_status=BookingStatus.BOOKED,
        lab_session__starts_at__lt=session.ends_at,
        lab_session__ends_at__gt=session.starts_at,
        **lookup,
    ).exclude(lab_session_id=session.pk).count()


def room_bookings_on_other_sessions(session: LabSession) -> int:
    return _overlapping_booked_count(session, lab_session__room_id=session.room_id)


def lab_work_bookings_on_other_sessions(session: LabSession) -> int:
    return _overlapping_booked_count(session, lab_session__lab_work_id=session.lab_work_id)


def session_available_seats(session: LabSession) -> int:
    from apps.bookings.models import BookingStatus

    session_booked = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    if session.is_stand_blocked_by_other_lab_work():
        return 0

    session_remaining = session.capacity - session_booked
    same_lab_other_booked = lab_work_bookings_on_other_sessions(session)
    lab_remaining = session.lab_work.capacity - same_lab_other_booked - session_booked

    other_room_booked = room_bookings_on_other_sessions(session)
    if other_room_booked == 0:
        return max(0, min(session_remaining, lab_remaining))
    room_remaining = session.room.capacity - other_room_booked - session_booked
    return max(0, min(session_remaining, lab_remaining, room_remaining))


def room_capacity_would_be_exceeded(session: LabSession, *, extra_bookings: int = 1) -> bool:
    from apps.bookings.models import BookingStatus

    other_booked = room_bookings_on_other_sessions(session)
    if other_booked == 0:
        return False
    session_booked = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    return other_booked + session_booked + extra_bookings > session.room.capacity


def lab_work_capacity_would_be_exceeded(session: LabSession, *, extra_bookings: int = 1) -> bool:
    from apps.bookings.models import BookingStatus

    other_booked = lab_work_bookings_on_other_sessions(session)
    session_booked = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    return other_booked + session_booked + extra_bookings > session.lab_work.capacity


# --- Кверисайты слотов ----------------------------------------------------------


def _filter_sessions_with_free_seats(qs: QuerySet[LabSession]) -> QuerySet[LabSession]:
    now = timezone.now()
    session_ids = []
    for session in qs:
        local_date = timezone.localtime(session.starts_at).date()
        laboratory_id = session.room.laboratory_id
        if not is_pair_time_for_booking(session.starts_at):
            continue
        if not is_day_open_for_booking(local_date, now, laboratory_id=laboratory_id):
            continue
        if not is_before_restriction_deadline(session.starts_at, now, laboratory_id=laboratory_id):
            continue
        if session.available_seats <= 0:
            continue
        session_ids.append(session.pk)
    if not session_ids:
        return qs.none()
    return qs.filter(pk__in=session_ids).order_by("starts_at")


def bookable_sessions_qs(lab_work_id: int | None = None, *, student=None) -> QuerySet[LabSession]:
    """Слоты, доступные студенту для записи: окно + праздники + места + whitelist + занятость."""
    now = timezone.now()
    min_date, max_date = booking_date_window(now)
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))

    qs = (
        LabSession.objects.filter(
            status=LabSessionStatus.OPEN,
            starts_at__gt=now,
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

    qs = _filter_sessions_with_free_seats(qs)
    qs = _filter_sessions_by_schedule_whitelist(qs, student=student)
    if student is not None:
        from apps.bookings.models import BookingStatus

        busy_intervals = [
            (booking.lab_session.starts_at, booking.lab_session.ends_at)
            for booking in student.bookings.filter(current_status=BookingStatus.BOOKED)
            .select_related("lab_session")
        ]
        if busy_intervals:
            session_ids = [
                session.pk
                for session in qs
                if not any(start < session.ends_at and end > session.starts_at for start, end in busy_intervals)
            ]
            qs = qs.filter(pk__in=session_ids) if session_ids else qs.none()
    return qs


def staff_manual_sessions_qs(lab_work_id: int) -> QuerySet[LabSession]:
    """Слоты для ручной записи: с текущего момента до N рабочих недель,
    без фильтра по свободным местам; пары и праздники учитываются."""
    now = timezone.now()
    max_date = manual_booking_max_date(now)
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))
    qs = (
        LabSession.objects.filter(
            status=LabSessionStatus.OPEN,
            ends_at__gt=now,
            starts_at__date__lte=max_date,
            lab_work_id=lab_work_id,
            room__is_blocked=False,
        )
        .select_related("lab_work", "room", "room__training_center")
        .order_by("starts_at")
    )
    session_ids = [
        session.pk
        for session in qs
        if is_session_in_manual_booking_window(session, now, max_date=max_date, holiday_dates=holiday_dates)
    ]
    if not session_ids:
        return qs.none()
    return qs.filter(pk__in=session_ids)
