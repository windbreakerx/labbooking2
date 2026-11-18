"""Тесты LabWorkBookingRelease (открытие записи по дате)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.academics.models import LabWorkBookingRelease
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.session_availability import is_booking_release_open
from apps.bookings.services.session_catalog import booking_release_status
from apps.scheduling.models import LabSession, LabSessionStatus
from tests.conftest import create_schedule_entry_for_session, next_open_weekday_pair


@pytest.mark.django_db
class TestBookingRelease:
    def test_closed_release_blocks_booking(self, lab_work, room, semester, student):
        starts = next_open_weekday_pair(days_ahead=3)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        # release отсутствует
        with pytest.raises(BookingError, match="запись"):
            BookingService(actor=student).create_booking(student, session.pk)

    def test_future_release_blocks_until_date(self, lab_work, room, semester, student):
        starts = next_open_weekday_pair(days_ahead=3)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        LabWorkBookingRelease.objects.create(
            lab_work=lab_work,
            semester=semester,
            booking_opens_at=timezone.now() + timedelta(days=1),
        )
        with pytest.raises(BookingError, match="запись"):
            BookingService(actor=student).create_booking(student, session.pk)

    def test_open_release_allows_booking(self, lab_work, room, semester, student):
        starts = next_open_weekday_pair(days_ahead=3)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session)
        booking = BookingService(actor=student).create_booking(student, session.pk)
        assert booking.current_status == "BOOKED"

    def test_release_status_helper(self, lab_work, room, semester):
        status = booking_release_status(lab_work.pk, semester.pk)
        assert status["state"] == "closed"

        future = timezone.now() + timedelta(days=1)
        LabWorkBookingRelease.objects.create(
            lab_work=lab_work, semester=semester, booking_opens_at=future
        )
        status = booking_release_status(lab_work.pk, semester.pk)
        assert status["state"] == "future"

    def test_is_booking_release_open_with_null(self, lab_work, room, semester):
        LabWorkBookingRelease.objects.create(
            lab_work=lab_work, semester=semester, booking_opens_at=None
        )
        assert is_booking_release_open(lab_work.pk, semester.pk) is False
