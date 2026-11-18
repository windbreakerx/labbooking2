"""Тесты CRUD одного слота (session_admin)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.academics.models import LabWork
from apps.bookings.models import BookingStatus
from apps.bookings.services import BookingService
from apps.scheduling.models import LabSessionBookingMode, LabSessionStatus
from apps.scheduling.services.session_admin import (
    SessionError,
    cancel_session,
    create_session,
    update_session,
)
from tests.conftest import next_open_weekday_pair


def _session_payload(lab_work, room, semester, starts_at, **overrides):
    return {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "date": starts_at.date().isoformat(),
        "time": starts_at.time().strftime("%H:%M"),
        **overrides,
    }


@pytest.mark.django_db
class TestSessionAdmin:
    def test_create_session(self, lab_work, room, semester, staff):
        starts = next_open_weekday_pair(days_ahead=3)
        session = create_session(
            room.laboratory,
            _session_payload(lab_work, room, semester, starts),
            actor=staff,
        )
        assert session.status == LabSessionStatus.OPEN
        assert session.lab_work == lab_work
        assert session.capacity == lab_work.capacity

    def test_create_session_in_past_fails(self, lab_work, room, semester, staff):
        past = timezone.now() - timedelta(days=1)
        with pytest.raises(SessionError, match="прошлом"):
            create_session(
                room.laboratory,
                _session_payload(lab_work, room, semester, past),
                actor=staff,
            )

    def test_create_group_session_auto_book_students(
        self, lab_work, room, semester, staff, student_group
    ):
        from tests.conftest import make_student

        make_student("g1@stud.spmi.ru", student_group)
        make_student("g2@stud.spmi.ru", student_group)
        starts = next_open_weekday_pair(days_ahead=3)
        session = create_session(
            room.laboratory,
            _session_payload(
                lab_work,
                room,
                semester,
                starts,
                booking_mode=LabSessionBookingMode.GROUP,
                occupant_group_id=student_group.pk,
            ),
            actor=staff,
        )
        assert session.booking_mode == LabSessionBookingMode.GROUP
        assert session.capacity == student_group.students.count()
        bookings = session.bookings.filter(current_status=BookingStatus.BOOKED)
        assert bookings.count() == student_group.students.count()

    def test_update_session_keeps_bookings(self, lab_work, room, semester, staff, student):
        starts = next_open_weekday_pair(days_ahead=3)
        session = create_session(
            room.laboratory,
            _session_payload(lab_work, room, semester, starts),
            actor=staff,
        )
        BookingService(actor=student).create_booking(student, session.pk)
        update_session(
            room.laboratory,
            session,
            _session_payload(lab_work, room, semester, starts),
            actor=staff,
        )
        session.refresh_from_db()
        assert session.capacity == lab_work.capacity
        assert session.bookings.filter(current_status=BookingStatus.BOOKED).count() == 1

    def test_update_session_time_with_bookings_fails(self, lab_work, room, semester, staff, student):
        starts = next_open_weekday_pair(days_ahead=3)
        session = create_session(
            room.laboratory,
            _session_payload(lab_work, room, semester, starts),
            actor=staff,
        )
        BookingService(actor=student).create_booking(student, session.pk)
        new_starts = starts + timedelta(hours=2)
        with pytest.raises(SessionError, match="время начала"):
            update_session(
                room.laboratory,
                session,
                _session_payload(lab_work, room, semester, new_starts),
                actor=staff,
            )

    def test_cancel_session_closes_and_cancels_bookings(
        self, lab_work, room, semester, staff, student
    ):
        starts = next_open_weekday_pair(days_ahead=3)
        session = create_session(
            room.laboratory,
            _session_payload(lab_work, room, semester, starts),
            actor=staff,
        )
        BookingService(actor=student).create_booking(student, session.pk)
        cancel_session(room.laboratory, session, actor=staff)
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        assert session.bookings.filter(
            current_status=BookingStatus.SLOT_CANCELLED
        ).exists()

    def test_create_session_validates_lab_work_belongs_to_lab(
        self, room, semester, staff
    ):
        from apps.academics.models import Discipline

        other_discipline = Discipline.objects.create(
            title="Чужая", semester=semester, is_published=True
        )
        other_work = LabWork.objects.create(
            number=99, title="Чужая ЛР", duration_minutes=90, is_published=True
        )
        other_work.disciplines.add(other_discipline)
        starts = next_open_weekday_pair(days_ahead=3)
        with pytest.raises(SessionError, match="лаборатории"):
            create_session(
                room.laboratory,
                _session_payload(other_work, room, semester, starts),
                actor=staff,
            )
