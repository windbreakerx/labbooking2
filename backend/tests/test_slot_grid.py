"""Фундамент слотов: сетка пар, академическая чётность, уникальность слота.

Чётность — решение 08.09: непрерывный счёт недель учебного года с недели,
содержащей 1 сентября (неделя №1 нечётная), счёт сквозной через каникулы
и Новый год; ISO-чётность v1 не переносится. 01.09.2026 — вторник,
неделя №1 = 31.08–06.09.2026.
"""

from datetime import date, datetime, time, timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.bookings.services.session_availability import (
    is_pair_time_for_booking,
    session_matches_schedule_whitelist,
)
from apps.scheduling.models import (
    LabSession,
    LabSessionStatus,
    Room,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
    WeekParity,
)
from apps.scheduling.services.slot_grid import (
    BOOKING_START_GRID_MINUTES,
    UNIVERSITY_PAIR_SLOTS,
    academic_week_parity,
    minutes_between,
    pair_start_times_for_duration,
    time_to_minutes,
)

# --- Сетка пар -------------------------------------------------------------------


@pytest.mark.parametrize("duration_minutes", [30, 45, 60, 90])
def test_pair_starts_fit_inside_one_pair(duration_minutes):
    for start in pair_start_times_for_duration(duration_minutes):
        start_minutes = time_to_minutes(start)
        containing = [
            (pair_start, pair_end)
            for _, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS
            if time_to_minutes(pair_start) <= start_minutes < time_to_minutes(pair_end)
        ]
        assert len(containing) == 1
        pair_start, pair_end = containing[0]
        offset = start_minutes - time_to_minutes(pair_start)
        assert offset % BOOKING_START_GRID_MINUTES == 0
        assert offset + duration_minutes <= minutes_between(pair_start, pair_end)


def test_ninety_minute_works_start_only_at_full_pairs():
    # пара 5 длится 85 минут — 90-минутная ЛР в неё не помещается
    assert pair_start_times_for_duration(90) == [
        time(8, 50),
        time(10, 35),
        time(12, 35),
        time(14, 15),
        time(17, 30),
    ]


def test_thirty_minute_works_cover_grid_with_step():
    starts = pair_start_times_for_duration(30)
    assert len(starts) == 29  # 5 полных пар × 5 стартов + 85-минутная пара × 4
    assert starts[0] == time(8, 50)
    assert starts[-1] == time(18, 30)


@pytest.mark.parametrize("duration_minutes", [30, 45, 60, 90])
def test_generated_starts_are_bookable_pair_times(duration_minutes):
    tz = timezone.get_current_timezone()
    for start in pair_start_times_for_duration(duration_minutes):
        moment = timezone.make_aware(datetime.combine(date(2026, 9, 9), start), tz)
        assert is_pair_time_for_booking(moment)


# --- Чётность учебных недель ------------------------------------------------------


def test_week_with_september_first_is_week_one():
    for day in (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 6)):
        assert academic_week_parity(day) == WeekParity.ODD
    assert academic_week_parity(date(2026, 9, 7)) == WeekParity.EVEN


def test_parity_is_continuous_through_new_year():
    # 28.12.2026 (пн) – 03.01.2027 (вс) — одна учебная неделя №18, чётная
    for day in (date(2026, 12, 28), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 3)):
        assert academic_week_parity(day) == WeekParity.EVEN


def test_spring_semester_continues_autumn_parity():
    # решение дня: 08.02.2027 — 24-я учебная неделя, чётная
    assert academic_week_parity(date(2027, 2, 7)) == WeekParity.ODD
    assert academic_week_parity(date(2027, 2, 8)) == WeekParity.EVEN


def test_dates_before_september_count_previous_academic_year():
    # 01.09.2025 — понедельник: неделя с него — №1 учебного года 2025/26
    assert academic_week_parity(date(2025, 9, 1)) == WeekParity.ODD
    assert academic_week_parity(date(2026, 5, 6)) == WeekParity.EVEN  # 36-я неделя


# --- Whitelist по академической чётности ------------------------------------------


def _session_on(lab_work, room, semester, day):
    tz = timezone.get_current_timezone()
    starts = timezone.make_aware(datetime.combine(day, time(10, 35)), tz)
    return LabSession.objects.create(
        lab_work=lab_work,
        room=room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=90),
        capacity=2,
        status=LabSessionStatus.OPEN,
    )


def test_whitelist_matches_parity_by_academic_week_count(lab_work, room, semester):
    """Нечётная запись пропускает 23-ю учебную неделю, но не 24-ю.

    На 01.02.2027 ISO-чётность дала бы обратный ответ (чётная неделя) —
    тест фиксирует именно академическую разметку v2.
    """
    entry = ScheduleEntry.objects.create(
        room=room,
        semester=semester,
        week_parity=WeekParity.ODD,
        weekday=0,
        start_time=time(10, 35),
        duration_minutes=90,
        capacity=2,
    )
    selection = ScheduleEntryDisciplineSelection.objects.create(
        schedule_entry=entry, discipline=lab_work.disciplines.first()
    )
    selection.lab_works.set([lab_work])

    odd_monday = _session_on(lab_work, room, semester, date(2027, 2, 1))
    even_monday = _session_on(lab_work, room, semester, date(2027, 2, 8))

    assert session_matches_schedule_whitelist(odd_monday) is True
    assert session_matches_schedule_whitelist(even_monday) is False


# --- Уникальность слота и флаг аудитории -------------------------------------------


def test_duplicate_slot_is_rejected_by_unique_constraint(session):
    with pytest.raises(IntegrityError):
        LabSession.objects.create(
            lab_work=session.lab_work,
            room=session.room,
            semester=session.semester,
            starts_at=session.starts_at,
            ends_at=session.ends_at + timedelta(minutes=30),
            capacity=1,
            status=LabSessionStatus.CLOSED,
        )


def test_same_start_in_other_room_is_allowed(session):
    other = Room.objects.create(
        training_center=session.room.training_center, number="102", capacity=2
    )
    clone = LabSession.objects.create(
        lab_work=session.lab_work,
        room=other,
        semester=session.semester,
        starts_at=session.starts_at,
        ends_at=session.ends_at,
        capacity=1,
        status=LabSessionStatus.OPEN,
    )
    assert clone.pk is not None


def test_room_autogen_exclusion_defaults_to_off(room):
    assert room.is_excluded_from_autogen is False
