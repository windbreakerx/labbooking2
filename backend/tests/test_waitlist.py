"""Очередь на слот: промоушен после коммита, выбытие, сброс при отмене слота."""

import logging

import pytest
from django.db.utils import OperationalError
from django.utils import timezone

from apps.bookings.models import BookingStatus, WaitlistEntry
from apps.bookings.services import BookingService, join_waitlist
from apps.scheduling.models import LabSession, LabSessionStatus

from .conftest import create_schedule_entry_for_session, make_student


@pytest.mark.django_db
class TestWaitlist:
    def test_cancel_promotes_first_after_commit(
        self, student, session, staff, student_group, django_capture_on_commit_callbacks
    ):
        other = make_student("promoted@stud.spmi.ru", student_group)
        session.capacity = 1
        session.save(update_fields=["capacity"])
        booking = BookingService(actor=student).create_booking(student, session.pk)
        join_waitlist(other, session.pk)

        with django_capture_on_commit_callbacks(execute=True):
            BookingService(actor=staff).cancel_booking(
                booking, by_staff=True, note="Освобождение места"
            )

        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED
        assert not WaitlistEntry.objects.filter(lab_session=session).exists()
        assert other.bookings.filter(
            lab_session=session, current_status=BookingStatus.BOOKED
        ).exists()

    def test_promotion_failure_drops_entry_and_keeps_cancel(
        self,
        student,
        session,
        staff,
        lab_work,
        room,
        semester,
        student_group,
        caplog,
        django_capture_on_commit_callbacks,
    ):
        other = make_student("dropped@stud.spmi.ru", student_group)
        session.capacity = 1
        session.save(update_fields=["capacity"])
        booking = BookingService(actor=student).create_booking(student, session.pk)

        # Пересекающийся слот: у other активная запись на то же время —
        # промоушен упадёт на лимите дисциплины/пересечении.
        overlapping = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=session.starts_at + timezone.timedelta(minutes=15),
            ends_at=session.starts_at + timezone.timedelta(minutes=105),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(overlapping, lab_work=lab_work)
        BookingService(actor=other).create_booking(other, overlapping.pk)

        join_waitlist(other, session.pk)
        with caplog.at_level(logging.INFO, logger="apps.bookings.notifications"):
            with django_capture_on_commit_callbacks(execute=True):
                BookingService(actor=staff).cancel_booking(
                    booking, by_staff=True, note="Освобождение места"
                )

        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED  # отмена не откатилась
        assert not WaitlistEntry.objects.filter(lab_session=session).exists()
        assert not other.bookings.filter(lab_session=session).exists()
        assert "Вы выбыли из очереди" in caplog.text

    def test_cancel_session_drops_queue_without_promotion(
        self, student, session, staff, student_group, caplog, django_capture_on_commit_callbacks
    ):
        other = make_student("queued@stud.spmi.ru", student_group)
        session.capacity = 1
        session.save(update_fields=["capacity"])
        BookingService(actor=student).create_booking(student, session.pk)
        join_waitlist(other, session.pk)

        with caplog.at_level(logging.INFO, logger="apps.bookings.notifications"):
            with django_capture_on_commit_callbacks(execute=True):
                BookingService(actor=staff).cancel_session_bookings(session, note="Снятие ЛР")

        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        assert not WaitlistEntry.objects.filter(lab_session=session).exists()
        assert not other.bookings.exists()  # промоушена не было — слот отменён
        assert "очередь сброшена" in caplog.text

    def test_cancel_retries_on_deadlock(self, student, session, monkeypatch):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        attempts = {"count": 0}
        original_cancel = BookingService._cancel_booking_impl

        def flaky_cancel(self, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OperationalError("deadlock detected")
            return original_cancel(self, *args, **kwargs)

        monkeypatch.setattr(BookingService, "_cancel_booking_impl", flaky_cancel)
        monkeypatch.setattr("apps.bookings.services.retry.time.sleep", lambda *_args: None)

        BookingService(actor=student).cancel_booking(booking)
        booking.refresh_from_db()
        assert attempts["count"] == 2
        assert booking.current_status == BookingStatus.CANCELLED
