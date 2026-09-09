"""Генератор слотов: insert-only, парность, праздники, идемпотентность.

Фиксированный ``now`` (Пн 07.09.2026 12:00) даёт детерминированные даты:
учебные недели №1 (31.08–06.09) и №3 (14.09–20.09) — нечётные, №2 и №4 —
чётные; горизонт по умолчанию 15 дней = 08.09–22.09, вторники в нём:
08.09 (чёт.), 15.09 (нечёт.), 22.09 (чёт.).
"""

from datetime import date, datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.scheduling.models import (
    Holiday,
    LabSession,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
)
from apps.scheduling.services.slot_generation import generate_lab_sessions
from apps.users.models import User, UserRole

from .conftest import create_lab_work

NOW = timezone.make_aware(datetime(2026, 9, 7, 12, 0))  # Пн, неделя №2 (чётная)
TUESDAYS = (date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22))


def make_entry(room, semester, discipline, *lab_works, weekday=1, capacity=30, week_parity="BOTH", teacher=None):
    entry = ScheduleEntry.objects.create(
        room=room,
        semester=semester,
        weekday=weekday,
        start_time="10:35",
        duration_minutes=90,
        capacity=capacity,
        week_parity=week_parity,
        teacher=teacher,
    )
    if lab_works:
        selection = ScheduleEntryDisciplineSelection.objects.create(
            schedule_entry=entry, discipline=discipline
        )
        selection.lab_works.set(lab_works)
    else:
        ScheduleEntryDisciplineSelection.objects.create(schedule_entry=entry, discipline=discipline)
    return entry


def session_dates():
    return sorted(
        timezone.localtime(session.starts_at).date() for session in LabSession.objects.all()
    )


def test_generates_one_slot_per_pair(lab_work, room, semester, discipline):
    make_entry(room, semester, discipline, lab_work)
    created = generate_lab_sessions(semester=semester, now=NOW)
    sessions = LabSession.objects.order_by("starts_at")
    assert created == 3
    assert session_dates() == list(TUESDAYS)
    for session in sessions:
        local = timezone.localtime(session.starts_at)
        assert local.strftime("%H:%M") == "10:35"
        assert timezone.localtime(session.ends_at).strftime("%H:%M") == "12:05"
        assert session.capacity == 30
        assert session.status == "OPEN"
        assert session.room_id == room.pk
        assert session.semester_id == semester.pk


def test_grid_starts_for_short_lab_works(room, semester, discipline, lab_work):
    short = create_lab_work(discipline, number=2, title="ЛР 2", duration_minutes=30)
    make_entry(room, semester, discipline, short)
    generate_lab_sessions(semester=semester, now=NOW)
    starts = sorted(
        timezone.localtime(s.starts_at).strftime("%H:%M")
        for s in LabSession.objects.all()
        if timezone.localtime(s.starts_at).date() == TUESDAYS[0]
    )
    assert starts == ["10:35", "10:50", "11:05", "11:20", "11:35"]
    assert LabSession.objects.count() == 15


def test_odd_parity_only_odd_weeks(lab_work, room, semester, discipline):
    make_entry(room, semester, discipline, lab_work, week_parity="ODD")
    created = generate_lab_sessions(semester=semester, now=NOW)
    assert created == 1
    assert session_dates() == [date(2026, 9, 15)]


def test_even_parity_only_even_weeks(lab_work, room, semester, discipline):
    make_entry(room, semester, discipline, lab_work, week_parity="EVEN")
    created = generate_lab_sessions(semester=semester, now=NOW)
    assert created == 2
    assert session_dates() == [date(2026, 9, 8), date(2026, 9, 22)]


def test_holiday_dates_skipped(lab_work, room, semester, discipline):
    Holiday.objects.create(date=date(2026, 9, 15), name="Тест")
    make_entry(room, semester, discipline, lab_work, week_parity="ODD")
    assert generate_lab_sessions(semester=semester, now=NOW) == 0


def test_excluded_room_skipped(lab_work, room, semester, discipline):
    room.is_excluded_from_autogen = True
    room.save(update_fields=["is_excluded_from_autogen"])
    make_entry(room, semester, discipline, lab_work)
    assert generate_lab_sessions(semester=semester, now=NOW) == 0


def test_blocked_room_skipped(lab_work, room, semester, discipline):
    room.is_blocked = True
    room.save(update_fields=["is_blocked"])
    make_entry(room, semester, discipline, lab_work)
    assert generate_lab_sessions(semester=semester, now=NOW) == 0


def test_unpublished_lab_work_skipped(lab_work, room, semester, discipline):
    lab_work.is_published = False
    lab_work.save(update_fields=["is_published"])
    make_entry(room, semester, discipline, lab_work)
    assert generate_lab_sessions(semester=semester, now=NOW) == 0


def test_empty_selection_means_all_discipline_lab_works(room, semester, discipline, lab_work):
    short = create_lab_work(discipline, number=2, title="ЛР 2", duration_minutes=30)
    make_entry(room, semester, discipline)  # выборка без ЛР = все ЛР дисциплины
    generate_lab_sessions(semester=semester, now=NOW)
    works = set(LabSession.objects.values_list("lab_work_id", flat=True))
    assert works == {lab_work.pk, short.pk}
    assert LabSession.objects.count() == 18  # 3 слота 90-мин + 15 слотов 30-мин


def test_slots_always_in_future(lab_work, room, semester, discipline):
    make_entry(room, semester, discipline, lab_work)
    tuesday_noon = timezone.make_aware(datetime(2026, 9, 8, 12, 0))
    created = generate_lab_sessions(semester=semester, now=tuesday_noon)
    assert created == 2  # 08.09 просрочен, остаются 15.09 и 22.09
    assert all(s.starts_at > tuesday_noon for s in LabSession.objects.all())


def test_idempotent_and_cancelled_not_resurrected(lab_work, room, semester, discipline):
    make_entry(room, semester, discipline, lab_work)
    assert generate_lab_sessions(semester=semester, now=NOW) == 3
    assert generate_lab_sessions(semester=semester, now=NOW) == 0

    first = LabSession.objects.order_by("starts_at").first()
    first.status = "CANCELLED"
    first.save(update_fields=["status"])
    assert generate_lab_sessions(semester=semester, now=NOW) == 0
    first.refresh_from_db()
    assert first.status == "CANCELLED"
    assert LabSession.objects.count() == 3


def test_capacity_is_min_of_entry_and_lab_work(lab_work, room, semester, discipline):
    lab_work.capacity = 5
    lab_work.save(update_fields=["capacity"])
    make_entry(room, semester, discipline, lab_work, capacity=3)
    generate_lab_sessions(semester=semester, now=NOW)
    assert set(LabSession.objects.values_list("capacity", flat=True)) == {3}


def test_teacher_copied_from_entry(lab_work, room, semester, discipline):
    teacher = User.objects.create_user(
        email="t@spmi.ru", password="pass", first_name="Т", last_name="Преп", role=UserRole.TEACHER
    )
    make_entry(room, semester, discipline, lab_work, teacher=teacher)
    generate_lab_sessions(semester=semester, now=NOW)
    assert set(LabSession.objects.values_list("teacher_id", flat=True)) == {teacher.pk}


def test_lab_work_ids_filter(lab_work, room, semester, discipline):
    short = create_lab_work(discipline, number=2, title="ЛР 2", duration_minutes=30)
    make_entry(room, semester, discipline, lab_work, short)
    assert generate_lab_sessions(semester=semester, now=NOW, lab_work_ids=[lab_work.pk]) == 3
    assert generate_lab_sessions(semester=semester, now=NOW, lab_work_ids=[short.pk]) == 15


def test_weeks_argument_respects_horizon(lab_work, room, semester, discipline, monkeypatch):
    make_entry(room, semester, discipline, lab_work)
    monkeypatch.setattr("django.conf.settings.BOOKING_HORIZON_DAYS", 6)
    assert generate_lab_sessions(semester=semester, weeks=1, now=NOW) == 1
    assert session_dates() == [date(2026, 9, 8)]


def test_command_creates_slots(lab_work, room, semester, discipline):
    tomorrow_weekday = (timezone.now() + timedelta(days=1)).weekday()
    make_entry(room, semester, discipline, lab_work, weekday=tomorrow_weekday)
    stdout = StringIO()
    call_command("generate_sessions", "--weeks", "2", stdout=stdout)
    assert "Создано слотов" in stdout.getvalue()
    assert LabSession.objects.filter(status="OPEN").exists()


def test_command_requires_active_semester(lab_work, room, semester, discipline):
    semester.is_active = False
    semester.save(update_fields=["is_active"])
    with pytest.raises(CommandError, match="активного семестра"):
        call_command("generate_sessions")
