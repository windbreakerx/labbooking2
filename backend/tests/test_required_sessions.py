"""Тесты многосессионных лабораторных работ (LabWork.required_sessions)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.bookings.models import BookingStatus
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.session_catalog import student_bookable_slots
from apps.scheduling.models import LabSession, LabSessionStatus
from tests.conftest import create_schedule_entry_for_session, next_open_weekday_pair


@pytest.mark.django_db
class TestRequiredSessions:
    def test_student_can_book_up_to_required_sessions(
        self, lab_work, room, semester, student, student_group
    ):
        lab_work.required_sessions = 2
        lab_work.save(update_fields=["required_sessions"])

        starts1 = next_open_weekday_pair(days_ahead=3)
        s1 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts1,
            ends_at=starts1 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s1)

        starts2 = starts1 + timedelta(days=7)
        s2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s2)

        starts3 = starts2 + timedelta(days=7)
        s3 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts3,
            ends_at=starts3 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s3)

        BookingService(actor=student).create_booking(student, s1.pk)
        BookingService(actor=student).create_booking(student, s2.pk)
        with pytest.raises(BookingError, match="требуется"):
            BookingService(actor=student).create_booking(student, s3.pk)

    def test_visited_session_frees_slot_for_rebooking(
        self, lab_work, room, semester, student, staff
    ):
        lab_work.required_sessions = 1
        lab_work.save(update_fields=["required_sessions"])

        starts1 = next_open_weekday_pair(days_ahead=3)
        s1 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts1,
            ends_at=starts1 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s1)
        booking = BookingService(actor=student).create_booking(student, s1.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.VISITED)

        starts2 = starts1 + timedelta(days=7)
        s2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s2)
        rebooked = BookingService(actor=student).create_booking(student, s2.pk)
        assert rebooked.current_status == BookingStatus.BOOKED

    def test_catalog_counts_active_not_visited(
        self, lab_work, room, semester, student, staff
    ):
        lab_work.required_sessions = 1
        lab_work.save(update_fields=["required_sessions"])

        starts1 = next_open_weekday_pair(days_ahead=3)
        s1 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts1,
            ends_at=starts1 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s1)
        booking = BookingService(actor=student).create_booking(student, s1.pk)
        slots = student_bookable_slots(lab_work.pk, student=student)
        assert not any(slot.session.pk == s1.pk for slot in slots)

        BookingService(actor=staff).change_status(booking, BookingStatus.VISITED)
        slots = student_bookable_slots(lab_work.pk, student=student)
        assert any(slot.session.pk == s1.pk for slot in slots)
