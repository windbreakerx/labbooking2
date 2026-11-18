"""Целостность данных: уникальность записи, PROTECT, архив, вместимости."""

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from apps.academics.scope import (
    published_disciplines_qs,
    student_can_access_lab_work,
    student_disciplines_qs,
    student_lab_works_qs,
)
from apps.bookings.models import AuditLog, Booking, BookingStatus
from apps.bookings.services import BookingService
from apps.scheduling.models import LabStand, Room
from apps.scheduling.services.catalog_admin import update_room
from apps.scheduling.services.session_admin import SessionError, create_session

from .conftest import make_student, next_open_weekday_pair, seed_manual_booking


def _session_payload(lab_work, room, semester, starts_at):
    return {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "date": starts_at.date().isoformat(),
        "time": starts_at.time().strftime("%H:%M"),
    }


@pytest.mark.django_db
class TestActiveBookingUniqueness:
    def test_second_active_booking_same_slot_is_rejected(self, student, session, staff):
        BookingService(actor=student).create_booking(student, session.pk)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                seed_manual_booking(actor=staff, student=student, session=session)

    def test_rebooking_after_cancel_is_allowed(
        self, student, session, staff, django_capture_on_commit_callbacks
    ):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).cancel_booking(
            booking, by_staff=True, note="Освобождение места"
        )
        rebooked = BookingService(actor=student).create_booking(student, session.pk)
        assert rebooked.current_status == BookingStatus.BOOKED
        assert rebooked.pk != booking.pk


@pytest.mark.django_db
class TestProtectedHistory:
    def test_session_with_booking_cannot_be_deleted(self, student, session):
        BookingService(actor=student).create_booking(student, session.pk)
        with pytest.raises(ProtectedError):
            session.delete()

    def test_lab_work_with_booking_cannot_be_deleted(self, student, session, lab_work):
        BookingService(actor=student).create_booking(student, session.pk)
        with pytest.raises(ProtectedError):
            lab_work.delete()

    def test_discipline_with_booking_cannot_be_deleted(self, student, session, discipline):
        BookingService(actor=student).create_booking(student, session.pk)
        with pytest.raises(ProtectedError):
            discipline.delete()


@pytest.mark.django_db
class TestArchive:
    def test_archived_lab_work_is_hidden_from_student(
        self, student, session, lab_work, room, semester, staff
    ):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        lab_work.is_archived = True
        lab_work.save(update_fields=["is_archived"])

        assert not student_lab_works_qs(student).filter(pk=lab_work.pk).exists()
        assert not student_can_access_lab_work(student, lab_work.pk)
        # История записи не удалена и доступна сотруднику.
        assert Booking.objects.filter(pk=booking.pk).exists()

        starts = next_open_weekday_pair(days_ahead=5)
        with pytest.raises(SessionError, match="архив"):
            create_session(
                room.laboratory,
                _session_payload(lab_work, room, semester, starts),
                actor=staff,
            )

    def test_archived_discipline_is_hidden_from_student(self, student, discipline):
        discipline.is_archived = True
        discipline.save(update_fields=["is_archived"])

        assert not published_disciplines_qs().filter(pk=discipline.pk).exists()
        assert not student_disciplines_qs(student).filter(pk=discipline.pk).exists()


@pytest.mark.django_db
class TestCapacityConstraints:
    def test_room_capacity_must_be_positive(self, room):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Room.objects.create(
                    training_center=room.training_center, number="000", capacity=0
                )

    def test_stand_capacity_must_be_positive(self, room):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LabStand.objects.create(
                    name="Нулевой стенд",
                    inventory_number="ZERO-1",
                    training_center=room.training_center,
                    room=room,
                    capacity=0,
                )


@pytest.mark.django_db
class TestRoomCapacityChange:
    def test_decrease_reports_overloaded_sessions_and_writes_audit(
        self, student, student_group, session, room, staff
    ):
        second = make_student("room-overload@stud.spmi.ru", student_group)
        seed_manual_booking(actor=staff, student=student, session=session)
        seed_manual_booking(actor=staff, student=second, session=session)

        updated, overloaded = update_room(
            laboratory=room.laboratory,
            room=room,
            data={"number": room.number, "name": room.name, "capacity": "1"},
            actor=staff,
        )

        assert updated.capacity == 1
        assert [row["session"].pk for row in overloaded] == [session.pk]
        assert overloaded[0]["booked"] == 2

        log = AuditLog.objects.get(action="room.update", entity_id=room.pk)
        assert log.actor == staff
        assert log.payload == {"capacity_before": 2, "capacity_after": 1}
