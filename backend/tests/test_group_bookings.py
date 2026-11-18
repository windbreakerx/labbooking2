"""Тесты групповых броней (booking_mode=GROUP)."""

import pytest

from apps.bookings.models import BookingStatus
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.session_catalog import student_group_sessions
from apps.scheduling.models import LabSessionBookingMode
from apps.scheduling.services.session_admin import create_session
from tests.conftest import make_student, next_open_weekday_pair


def _group_session(lab_work, room, semester, staff, group):
    lab_work.laboratories.add(room.laboratory)
    starts = next_open_weekday_pair(days_ahead=3)
    return create_session(
        room.laboratory,
        {
            "lab_work_id": lab_work.pk,
            "room_id": room.pk,
            "semester_id": semester.pk,
            "date": starts.date().isoformat(),
            "time": starts.time().strftime("%H:%M"),
            "booking_mode": LabSessionBookingMode.GROUP,
            "occupant_group_id": group.pk,
        },
        actor=staff,
    )


@pytest.mark.django_db
class TestGroupBookings:
    def test_group_session_auto_books_all_students(
        self, lab_work, room, semester, staff, student_group
    ):
        s1 = make_student("g1@stud.spmi.ru", student_group)
        s2 = make_student("g2@stud.spmi.ru", student_group)
        session = _group_session(lab_work, room, semester, staff, student_group)
        assert session.booking_mode == LabSessionBookingMode.GROUP
        assert session.capacity == 2
        booked = list(session.bookings.filter(current_status=BookingStatus.BOOKED))
        assert {b.student_id for b in booked} == {s1.pk, s2.pk}

    def test_student_cannot_book_group_session_individually(
        self, lab_work, room, semester, staff, student, student_group
    ):
        session = _group_session(lab_work, room, semester, staff, student_group)
        with pytest.raises(BookingError):
            BookingService(actor=student).create_booking(student, session.pk)

    def test_group_session_visible_in_catalog(
        self, lab_work, room, semester, staff, student, student_group
    ):
        session = _group_session(lab_work, room, semester, staff, student_group)
        sessions = student_group_sessions(lab_work.pk, student)
        assert session in sessions

    def test_other_group_does_not_see_group_session(
        self, lab_work, room, semester, staff, student_group, discipline
    ):
        from apps.academics.models import StudentGroup

        make_student("member@stud.spmi.ru", student_group)
        other_group = StudentGroup.objects.create(name="OTHER-24")
        other_group.disciplines.add(discipline)
        other_student = make_student("other@stud.spmi.ru", other_group)
        session = _group_session(lab_work, room, semester, staff, student_group)
        sessions = student_group_sessions(lab_work.pk, other_student)
        assert session not in sessions
