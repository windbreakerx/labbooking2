"""Reconcile слотов: отмена осиротевших, синк параметров, анти-«зомби»."""

from django.utils import timezone

from apps.bookings.models import BookingStatus
from apps.bookings.services.schedule_sync import (
    sync_future_sessions_for_slot,
    sync_open_session_capacities,
    sync_open_session_durations,
)
from apps.scheduling.models import LabSession, LabSessionStatus, ScheduleEntry
from apps.users.models import User, UserRole

from .conftest import far_open_weekday_pair, seed_manual_booking


def session_entry(session):
    return ScheduleEntry.objects.get(room=session.room, semester=session.semester)


def test_orphan_session_cancelled_with_booking(session, student, staff):
    booking = seed_manual_booking(actor=staff, student=student, session=session)
    weekday = timezone.localtime(session.starts_at).weekday()
    session_entry(session).delete()

    cancelled = sync_future_sessions_for_slot(
        room=session.room, semester=session.semester, weekday=weekday
    )

    assert cancelled == 1
    session.refresh_from_db()
    booking.refresh_from_db()
    assert session.status == LabSessionStatus.CANCELLED
    assert booking.current_status == BookingStatus.SLOT_CANCELLED


def test_orphan_cancel_skips_other_weekday(session, student, staff):
    seed_manual_booking(actor=staff, student=student, session=session)
    weekday = timezone.localtime(session.starts_at).weekday()
    other_starts = far_open_weekday_pair(min_days_ahead=20)
    while other_starts.weekday() == weekday:
        other_starts += timezone.timedelta(days=1)
    other = LabSession.objects.create(
        lab_work=session.lab_work,
        room=session.room,
        semester=session.semester,
        starts_at=other_starts,
        ends_at=other_starts + timezone.timedelta(minutes=90),
        capacity=2,
        status=LabSessionStatus.OPEN,
    )
    session_entry(session).delete()

    sync_future_sessions_for_slot(room=session.room, semester=session.semester, weekday=weekday)

    other.refresh_from_db()
    assert other.status == LabSessionStatus.OPEN


def test_sync_updates_teacher_and_capacity_but_never_reopens(session):
    entry = session_entry(session)
    teacher = User.objects.create_user(
        email="t@spmi.ru", password="pass", first_name="Т", last_name="Преп", role=UserRole.TEACHER
    )
    entry.teacher = teacher
    entry.capacity = 1
    entry.save(update_fields=["teacher", "capacity"])

    sync_future_sessions_for_slot(
        room=session.room,
        semester=session.semester,
        weekday=timezone.localtime(session.starts_at).weekday(),
    )

    session.refresh_from_db()
    assert session.teacher_id == teacher.pk
    assert session.capacity == 1  # min(entry.capacity=1, lab_work.capacity=30)

    session.status = LabSessionStatus.CANCELLED
    session.save(update_fields=["status"])
    entry.capacity = 2
    entry.save(update_fields=["capacity"])
    sync_future_sessions_for_slot(
        room=session.room,
        semester=session.semester,
        weekday=timezone.localtime(session.starts_at).weekday(),
    )
    session.refresh_from_db()
    assert session.status == LabSessionStatus.CANCELLED
    assert session.capacity == 1


def test_sync_open_session_capacities(session):
    entry = session_entry(session)
    entry.capacity = 10
    entry.save(update_fields=["capacity"])
    session.lab_work.capacity = 5
    session.lab_work.save(update_fields=["capacity"])

    updated = sync_open_session_capacities(session.lab_work)

    assert updated == 1
    session.refresh_from_db()
    assert session.capacity == 5  # min(entry.capacity=10, lab_work.capacity=5)

    session.lab_work.capacity = 3
    session.lab_work.save(update_fields=["capacity"])
    sync_open_session_capacities(session.lab_work)
    session.refresh_from_db()
    assert session.capacity == 3


def test_sync_open_session_durations(session, student, staff):
    booked_starts = far_open_weekday_pair(min_days_ahead=10)
    while booked_starts.date() == timezone.localtime(session.starts_at).date():
        booked_starts += timezone.timedelta(days=1)
    booked = LabSession.objects.create(
        lab_work=session.lab_work,
        room=session.room,
        semester=session.semester,
        starts_at=booked_starts,
        ends_at=booked_starts + timezone.timedelta(minutes=90),
        capacity=2,
        status=LabSessionStatus.OPEN,
    )
    seed_manual_booking(actor=staff, student=student, session=booked)

    session.lab_work.duration_minutes = 60
    session.lab_work.save(update_fields=["duration_minutes"])
    updated = sync_open_session_durations(session.lab_work)

    assert updated == 1
    session.refresh_from_db()
    booked.refresh_from_db()
    assert session.ends_at == session.starts_at + timezone.timedelta(minutes=60)
    assert booked.ends_at == booked.starts_at + timezone.timedelta(minutes=90)
