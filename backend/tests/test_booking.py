"""Ядро записи: BookingService + SessionAvailability (порт из v1, web/API-тесты — Дни 5–7)."""

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from django.db.utils import OperationalError
from django.test import override_settings
from django.utils import timezone

from apps.academics.models import Discipline, StudentGroup
from apps.bookings.models import (
    NO_SHOW_EXPLANATION_THRESHOLD,
    BookingStatus,
    CancelSource,
    StudentLabAttendance,
)
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.attendance import (
    clear_explanation_required,
    mark_visited_for_ended_sessions,
)
from apps.bookings.services.session_availability import (
    bookable_sessions_qs,
    booking_date_window,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_pair_time_for_booking,
    is_ready_for_auto_visited,
)
from apps.scheduling.models import (
    AutoVisitedMode,
    Laboratory,
    LaboratoryBookingSettings,
    LabSession,
    LabSessionStatus,
    LabStand,
    Room,
    ScheduleEntry,
    TrainingCenter,
)
from apps.users.models import User, UserProfile, UserRole

from .conftest import (
    attach_schedule_entry_lab_works,
    create_lab_work,
    create_schedule_entry_for_session,
    next_open_weekday_pair,
)


def _assign_student_group(user, student_group):
    UserProfile.objects.update_or_create(user=user, defaults={"student_group": student_group})
    return user


def _make_student(email, student_group):
    return _assign_student_group(
        User.objects.create_user(
            email=email,
            password="pass",
            first_name=email[0].upper(),
            last_name=email.split("@")[0].title(),
            role=UserRole.STUDENT,
        ),
        student_group,
    )


@pytest.mark.django_db
class TestBookingService:
    def test_create_booking(self, student, session):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        assert booking.current_status == BookingStatus.BOOKED

    def test_capacity_limit(self, student, session, student_group):
        other = _make_student("o@stud.spmi.ru", student_group)
        third = _make_student("t@stud.spmi.ru", student_group)
        BookingService().create_booking(student, session.pk)
        BookingService().create_booking(other, session.pk)
        with pytest.raises(BookingError, match="Нет свободных мест"):
            BookingService().create_booking(third, session.pk)

    def test_one_booking_per_discipline(self, student, session, lab_work, room, semester):
        BookingService().create_booking(student, session.pk)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=session.starts_at,
            ends_at=session.starts_at + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        with pytest.raises(BookingError, match="активная запись"):
            BookingService().create_booking(student, session2.pk)

    def test_manual_booking_skips_limits(self, student, session, staff, lab_work, room, semester):
        BookingService().create_booking(student, session.pk)
        starts2 = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts2).weekday() >= 5:
            starts2 += timedelta(days=1)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        booking = BookingService(actor=staff).create_booking(student, session2.pk, manual=True)
        assert booking.registration_type == "MANUAL"

    def test_cancel_within_deadline(self, student, session):
        service = BookingService(actor=student)
        booking = service.create_booking(student, session.pk)
        cancelled = service.cancel_booking(booking)
        assert cancelled.current_status == BookingStatus.CANCELLED
        assert cancelled.cancel_source == CancelSource.STUDENT

    def test_staff_cancel_keeps_booking_unchanged(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        cancelled = BookingService(actor=staff).cancel_booking(
            booking, by_staff=True, note="Отмена по инициативе лаборатории"
        )
        assert cancelled.current_status == BookingStatus.CANCELLED
        assert cancelled.cancel_source == CancelSource.STAFF

    def test_student_can_rebook_after_no_show(
        self, student, session, staff, lab_work, room, semester
    ):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        starts2 = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts2).weekday() >= 5:
            starts2 += timedelta(days=1)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session2, lab_work=lab_work)
        rebooked = BookingService(actor=student).create_booking(student, session2.pk)
        assert rebooked.current_status == BookingStatus.BOOKED
        assert rebooked.is_repeat_after_no_show is True

    def test_no_show_blocks_only_that_discipline(
        self, student, session, staff, lab_work, room, semester, discipline, student_group
    ):
        discipline_b = Discipline.objects.create(
            title="Химия", semester=discipline.semester, is_published=True
        )
        student_group.disciplines.add(discipline_b)
        lab_work_b = create_lab_work(
            discipline_b, number=2, title="ЛР 2", duration_minutes=90, is_published=True
        )
        starts_b = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts_b).weekday() >= 5:
            starts_b += timedelta(days=1)
        session_b = LabSession.objects.create(
            lab_work=lab_work_b,
            room=room,
            semester=semester,
            starts_at=starts_b,
            ends_at=starts_b + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session_b, lab_work=lab_work_b)

        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)

        rebooked_same = BookingService(actor=student).create_booking(student, session.pk)
        assert rebooked_same.current_status == BookingStatus.BOOKED
        assert rebooked_same.is_repeat_after_no_show is True

        rebooked = BookingService(actor=student).create_booking(student, session_b.pk)
        assert rebooked.current_status == BookingStatus.BOOKED
        assert rebooked.is_repeat_after_no_show is False

    def test_student_can_rebook_after_self_cancel(self, student, session, lab_work, room, semester):
        service = BookingService(actor=student)
        booking = service.create_booking(student, session.pk)
        service.cancel_booking(booking)
        starts2 = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts2).weekday() >= 5:
            starts2 += timedelta(days=1)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session2, lab_work=lab_work)
        rebooked = BookingService(actor=student).create_booking(student, session2.pk)
        assert rebooked.current_status == BookingStatus.BOOKED

    def _lab_attendance(self, student, room):
        return StudentLabAttendance.objects.get(student=student, laboratory=room.laboratory)

    def test_no_show_records_lab_attendance(self, student, session, staff, room):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        booking.refresh_from_db()
        assert self._lab_attendance(student, room).no_show_count == 1
        assert booking.had_no_show is True

    def test_reaccess_keeps_no_show_history(
        self, student, session, staff, lab_work, room, semester
    ):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        BookingService(actor=staff).change_status(booking, BookingStatus.REACCESS)
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.REACCESS
        assert booking.had_no_show is True
        assert self._lab_attendance(student, room).no_show_count == 1
        starts2 = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts2).weekday() >= 5:
            starts2 += timedelta(days=1)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session2, lab_work=lab_work)
        rebooked = BookingService(actor=student).create_booking(student, session2.pk)
        assert rebooked.current_status == BookingStatus.BOOKED
        assert rebooked.had_no_show is False

    def test_staff_marks_booked_as_reaccess(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.REACCESS)
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.REACCESS
        assert booking.had_no_show is False

    def test_repeated_no_show_status_does_not_double_count(self, student, session, staff, room):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        booking.refresh_from_db()
        assert booking.had_no_show is True
        assert self._lab_attendance(student, room).no_show_count == 1

    def test_visited_after_no_show_clears_sticky_fact(self, student, session, staff, room):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        BookingService(actor=staff).change_status(
            booking, BookingStatus.VISITED, note="Студент присутствовал"
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.VISITED
        assert booking.had_no_show is False
        assert self._lab_attendance(student, room).no_show_count == 0

    def test_visited_after_reaccess_is_forbidden_keeps_sticky(self, student, session, staff, room):
        """REACCESS терминален; had_no_show остаётся на строке."""
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.NO_SHOW)
        BookingService(actor=staff).change_status(booking, BookingStatus.REACCESS)
        with pytest.raises(BookingError, match="конечн|недопустим"):
            BookingService(actor=staff).change_status(
                booking, BookingStatus.VISITED, note="Студент присутствовал"
            )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.REACCESS
        assert booking.had_no_show is True
        assert self._lab_attendance(student, room).no_show_count == 1

    def test_booked_to_reaccess_allowed(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(booking, BookingStatus.REACCESS)
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.REACCESS

    def test_slot_cancelled_does_not_touch_no_show_layers(self, student, session, staff, room):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).mark_slot_cancelled(booking, note="Поломка стенда")
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.SLOT_CANCELLED
        assert booking.had_no_show is False
        assert not StudentLabAttendance.objects.filter(
            student=student, laboratory=room.laboratory
        ).exists()

    def test_cancel_session_bookings_sets_slot_cancelled(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        count = BookingService(actor=staff).cancel_session_bookings(session, note="Снятие ЛР")
        booking.refresh_from_db()
        session.refresh_from_db()
        assert count == 1
        assert booking.current_status == BookingStatus.SLOT_CANCELLED
        assert session.status == LabSessionStatus.CANCELLED

    def test_cancelled_status_is_terminal(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=staff).change_status(
            booking, BookingStatus.CANCELLED, note="Студент не может посетить"
        )
        with pytest.raises(BookingError, match="конечн"):
            BookingService(actor=staff).change_status(
                booking, BookingStatus.VISITED, note="Студент присутствовал"
            )

    def test_three_no_shows_set_explanation_required(
        self, student, session, staff, lab_work, room, semester
    ):
        service_student = BookingService(actor=student)
        service_staff = BookingService(actor=staff)
        for i in range(NO_SHOW_EXPLANATION_THRESHOLD):
            starts = session.starts_at + timedelta(days=7 * (i + 1))
            while timezone.localtime(starts).weekday() >= 5:
                starts += timedelta(days=1)
            sess = LabSession.objects.create(
                lab_work=lab_work,
                room=room,
                semester=semester,
                starts_at=starts,
                ends_at=starts + timezone.timedelta(minutes=90),
                capacity=5,
                status=LabSessionStatus.OPEN,
            )
            create_schedule_entry_for_session(sess, lab_work=lab_work)
            booking = service_student.create_booking(student, sess.pk)
            service_staff.change_status(booking, BookingStatus.NO_SHOW)
            attendance = self._lab_attendance(student, room)
            assert attendance.no_show_count == i + 1
            assert attendance.explanation_required is (i + 1 >= NO_SHOW_EXPLANATION_THRESHOLD)

        clear_explanation_required(staff, student)
        attendance = self._lab_attendance(student, room)
        assert attendance.explanation_required is False
        assert attendance.no_show_count == NO_SHOW_EXPLANATION_THRESHOLD

    def test_no_shows_across_labs_do_not_trigger_explanation(
        self, student, session, staff, lab_work, room, semester
    ):
        other_lab = Laboratory.objects.create(
            training_center=room.training_center, name="Другая лаборатория"
        )
        other_room = Room.objects.create(
            training_center=room.training_center, number="202", capacity=5, laboratory=other_lab
        )
        third_lab = Laboratory.objects.create(
            training_center=room.training_center, name="Третья лаборатория"
        )
        third_room = Room.objects.create(
            training_center=room.training_center, number="303", capacity=5, laboratory=third_lab
        )
        service_student = BookingService(actor=student)
        service_staff = BookingService(actor=staff)

        booking1 = service_student.create_booking(student, session.pk)
        service_staff.change_status(booking1, BookingStatus.NO_SHOW)
        service_staff.change_status(booking1, BookingStatus.REACCESS)

        starts2 = session.starts_at + timedelta(days=7)
        while timezone.localtime(starts2).weekday() >= 5:
            starts2 += timedelta(days=1)
        session2 = LabSession.objects.create(
            lab_work=lab_work,
            room=other_room,
            semester=semester,
            starts_at=starts2,
            ends_at=starts2 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session2, lab_work=lab_work)
        booking2 = service_student.create_booking(student, session2.pk)
        service_staff.change_status(booking2, BookingStatus.NO_SHOW)
        service_staff.change_status(booking2, BookingStatus.REACCESS)

        starts3 = starts2 + timedelta(days=7)
        while timezone.localtime(starts3).weekday() >= 5:
            starts3 += timedelta(days=1)
        session3 = LabSession.objects.create(
            lab_work=lab_work,
            room=third_room,
            semester=semester,
            starts_at=starts3,
            ends_at=starts3 + timezone.timedelta(minutes=90),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session3, lab_work=lab_work)
        booking3 = service_student.create_booking(student, session3.pk)
        service_staff.change_status(booking3, BookingStatus.NO_SHOW)

        assert self._lab_attendance(student, room).no_show_count == 1
        assert self._lab_attendance(student, room).explanation_required is False
        assert StudentLabAttendance.objects.get(
            student=student, laboratory=other_lab
        ).no_show_count == 1
        assert StudentLabAttendance.objects.get(
            student=student, laboratory=third_lab
        ).no_show_count == 1
        assert not StudentLabAttendance.objects.filter(
            student=student, explanation_required=True
        ).exists()

    def test_staff_cancelled_via_status_promotes_waitlist(
        self, student, session, staff, student_group
    ):
        other = _make_student("waiter@stud.spmi.ru", student_group)
        session.capacity = 1
        session.save(update_fields=["capacity"])
        booking = BookingService(actor=student).create_booking(student, session.pk)
        BookingService(actor=other).join_waitlist(other, session.pk)
        BookingService(actor=staff).change_status(
            booking, BookingStatus.CANCELLED, note="Освобождение места"
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED
        assert other.bookings.filter(
            lab_session=session, current_status=BookingStatus.BOOKED
        ).exists()

    @override_settings(BOOKING_CANCEL_HOURS=200)
    def test_cancel_after_deadline_denied(self, student, session):
        service = BookingService(actor=student)
        booking = service.create_booking(student, session.pk)
        with pytest.raises(BookingError, match="200"):
            service.cancel_booking(booking)

    def test_room_parallel_capacity_limit(
        self, student, session, discipline, room, semester, student_group
    ):
        other_student = _make_student("s2@stud.spmi.ru", student_group)
        third_student = _make_student("s3@stud.spmi.ru", student_group)
        second_lab_work = create_lab_work(
            discipline, number=2, title="ЛР 2", duration_minutes=90, is_published=True
        )
        parallel_session = LabSession.objects.create(
            lab_work=second_lab_work,
            room=room,
            semester=semester,
            starts_at=session.starts_at,
            ends_at=session.ends_at,
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(parallel_session, lab_work=second_lab_work)
        BookingService().create_booking(student, session.pk)
        BookingService().create_booking(other_student, parallel_session.pk)
        with pytest.raises(BookingError, match="Аудитория"):
            BookingService().create_booking(third_student, parallel_session.pk)

    def test_same_lab_parallel_capacity_limit(
        self, student, session, room, semester, student_group
    ):
        room.capacity = 10
        room.save(update_fields=["capacity"])
        session.capacity = 3
        session.save(update_fields=["capacity"])
        session.lab_work.capacity = 3
        session.lab_work.save(update_fields=["capacity"])

        second_student = _make_student("same-lab-2@stud.spmi.ru", student_group)
        third_student = _make_student("same-lab-3@stud.spmi.ru", student_group)
        fourth_student = _make_student("same-lab-4@stud.spmi.ru", student_group)

        overlap = LabSession.objects.create(
            lab_work=session.lab_work,
            room=room,
            semester=semester,
            starts_at=session.starts_at + timezone.timedelta(minutes=30),
            ends_at=session.starts_at + timezone.timedelta(minutes=75),
            capacity=3,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(
            overlap, lab_work=session.lab_work, duration_minutes=45
        )

        BookingService().create_booking(student, session.pk)
        BookingService().create_booking(second_student, session.pk)
        BookingService().create_booking(third_student, session.pk)

        assert overlap.available_seats == 0
        with pytest.raises(BookingError, match="Лимит мест для этой лабораторной работы"):
            BookingService().create_booking(fourth_student, overlap.pk)

    def test_staff_status_change(self, student, session, staff):
        booking = BookingService(actor=student).create_booking(student, session.pk)
        updated = BookingService(actor=staff).change_status(
            booking, BookingStatus.VISITED, note="Студент присутствовал"
        )
        assert updated.current_status == BookingStatus.VISITED

    def test_booking_notifies_student(self, student, session, monkeypatch):
        events = []
        monkeypatch.setattr(
            "apps.bookings.services.booking.notify_booking_event",
            lambda booking, event: events.append(event),
        )
        BookingService(actor=student).create_booking(student, session.pk)
        assert events == ["booked"]

    def test_create_booking_calls_booking_lock(self, student, session, monkeypatch):
        called = {"count": 0, "session_id": None, "lock_ids": None}
        original_lock = BookingService._lock_for_booking

        def lock_spy(self, locked_session):
            called["count"] += 1
            called["session_id"] = locked_session.pk
            called["lock_ids"] = self._collect_booking_lock_ids(locked_session)
            return original_lock(self, locked_session)

        monkeypatch.setattr(BookingService, "_lock_for_booking", lock_spy)

        BookingService(actor=student).create_booking(student, session.pk)
        assert called["count"] == 1
        assert called["session_id"] == session.pk
        assert called["lock_ids"] == [session.pk]

    def test_create_booking_retries_on_deadlock(self, student, session, monkeypatch):
        attempts = {"count": 0}
        original_create = BookingService._create_booking_in_transaction

        def flaky_create(self, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OperationalError("deadlock detected")
            return original_create(self, *args, **kwargs)

        monkeypatch.setattr(BookingService, "_create_booking_in_transaction", flaky_create)
        monkeypatch.setattr("apps.bookings.services.booking.time.sleep", lambda *_args: None)

        booking = BookingService(actor=student).create_booking(student, session.pk)
        assert booking.pk
        assert attempts["count"] == 2

    def test_shared_stand_blocks_parallel_booking(
        self, student, session, room, semester, student_group
    ):
        stand = LabStand.objects.create(
            name="Общий стенд",
            inventory_number="ST-001",
            training_center=room.training_center,
            room=room,
        )
        session.lab_work.primary_stand = stand
        session.lab_work.save(update_fields=["primary_stand"])

        discipline_two = Discipline.objects.create(
            title="Вторая дисциплина", semester=semester, is_published=True
        )
        student_group.disciplines.add(discipline_two)
        second_lab = create_lab_work(
            discipline_two,
            number=1,
            title="ЛР на том же стенде",
            duration_minutes=90,
            is_published=True,
            primary_stand=stand,
        )
        second_session = LabSession.objects.create(
            lab_work=second_lab,
            room=room,
            semester=semester,
            starts_at=session.starts_at,
            ends_at=session.ends_at,
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(second_session, lab_work=second_lab)
        other_student = _make_student("stand-conflict@stud.spmi.ru", student_group)

        BookingService().create_booking(student, session.pk)
        with pytest.raises(BookingError, match="Стенд уже занят"):
            BookingService().create_booking(other_student, second_session.pk)

    def test_shared_stand_allows_multiple_students_on_same_lab_session(
        self, student, session, room, student_group
    ):
        stand = LabStand.objects.create(
            name="Стенд для группы",
            inventory_number="ST-002",
            training_center=room.training_center,
            room=room,
        )
        session.lab_work.primary_stand = stand
        session.lab_work.save(update_fields=["primary_stand"])
        session.capacity = 3
        session.save(update_fields=["capacity"])

        second_student = _make_student("stand-group@stud.spmi.ru", student_group)
        third_student = _make_student("stand-group2@stud.spmi.ru", student_group)

        BookingService().create_booking(student, session.pk)
        BookingService().create_booking(second_student, session.pk)
        BookingService().create_booking(third_student, session.pk)

        assert session.available_seats == 0

    def test_shared_stand_does_not_cap_available_seats_to_one(self, session, room):
        stand = LabStand.objects.create(
            name="Стенд без записей",
            inventory_number="ST-003",
            training_center=room.training_center,
            room=room,
        )
        session.lab_work.primary_stand = stand
        session.lab_work.save(update_fields=["primary_stand"])
        session.capacity = 3
        session.save(update_fields=["capacity"])

        assert session.available_seats == 3

    def test_student_cannot_book_overlapping_intervals(
        self, student, session, semester, student_group
    ):
        other_tc = TrainingCenter.objects.create(number=77)
        other_room = Room.objects.create(training_center=other_tc, number="777", capacity=10)
        discipline_two = Discipline.objects.create(
            title="Термодинамика", semester=semester, is_published=True
        )
        student_group.disciplines.add(discipline_two)
        second_lab = create_lab_work(
            discipline_two, number=1, title="ЛР 2", duration_minutes=60, is_published=True
        )
        overlap_start = session.starts_at + timezone.timedelta(minutes=30)
        overlapping_session = LabSession.objects.create(
            lab_work=second_lab,
            room=other_room,
            semester=semester,
            starts_at=overlap_start,
            ends_at=overlap_start + timezone.timedelta(minutes=60),
            capacity=5,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(
            overlapping_session, lab_work=second_lab, duration_minutes=60
        )
        BookingService().create_booking(student, session.pk)
        with pytest.raises(BookingError, match="пересекающееся время"):
            BookingService().create_booking(student, overlapping_session.pk)


@pytest.mark.django_db
class TestSessionAvailability:
    def test_horizon_excludes_far_sessions(self, session, far_session, lab_work):
        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert session in qs
        assert far_session not in qs

    def test_bookable_sessions_follow_schedule_whitelist(self, lab_work, room, semester):
        starts = next_open_weekday_pair(days_ahead=3, hour=10, minute=35)
        blocked_session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert blocked_session not in qs

        entry = ScheduleEntry.objects.create(
            room=room,
            semester=semester,
            week_parity="BOTH",
            weekday=timezone.localtime(starts).weekday(),
            start_time=timezone.localtime(starts).strftime("%H:%M"),
            duration_minutes=90,
            capacity=room.capacity,
            duty_role="TEACHER_DUTY",
            is_active=True,
        )
        attach_schedule_entry_lab_works(entry, lab_work)
        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert blocked_session in qs

    def test_schedule_whitelist_allows_offset_start_within_entry_window(
        self, lab_work, room, semester
    ):
        starts = next_open_weekday_pair(days_ahead=3, hour=11, minute=5)
        offset_session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=60),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        entry = ScheduleEntry.objects.create(
            room=room,
            semester=semester,
            week_parity="BOTH",
            weekday=timezone.localtime(starts).weekday(),
            start_time="10:35",
            duration_minutes=90,
            capacity=room.capacity,
            duty_role="TEACHER_DUTY",
            is_active=True,
        )
        attach_schedule_entry_lab_works(entry, lab_work)

        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert offset_session in qs

    def test_empty_selection_means_all_lab_works_of_discipline(self, lab_work, room, semester):
        """Пустая выборка ЛР в слоте расписания = разрешены все ЛР дисциплины."""
        starts = next_open_weekday_pair(days_ahead=3, hour=10, minute=35)
        sess = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        entry = ScheduleEntry.objects.create(
            room=room,
            semester=semester,
            week_parity="BOTH",
            weekday=timezone.localtime(starts).weekday(),
            start_time=timezone.localtime(starts).strftime("%H:%M"),
            duration_minutes=90,
            capacity=room.capacity,
            is_active=True,
        )
        from apps.scheduling.models import ScheduleEntryDisciplineSelection

        ScheduleEntryDisciplineSelection.objects.create(
            schedule_entry=entry, discipline=lab_work.disciplines.first()
        )  # без lab_works

        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert sess in qs

    def test_teacher_load_group_restricts_student_visibility(
        self, student, lab_work, room, semester
    ):
        starts = next_open_weekday_pair(days_ahead=3, hour=12, minute=35)
        restricted_session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        entry = ScheduleEntry.objects.create(
            room=room,
            semester=semester,
            week_parity="BOTH",
            weekday=timezone.localtime(starts).weekday(),
            start_time=timezone.localtime(starts).strftime("%H:%M"),
            duration_minutes=90,
            capacity=room.capacity,
            duty_role="TEACHER_DUTY",
            load_group_label="TEST-24",
            is_active=True,
        )
        attach_schedule_entry_lab_works(entry, lab_work)

        foreign_group = StudentGroup.objects.create(name="FOREIGN-24")
        foreign_group.disciplines.add(*student.profile.student_group.disciplines.all())
        outsider = User.objects.create_user(
            email="other-whitelist@stud.spmi.ru",
            password="pass",
            first_name="Other",
            last_name="Student",
            role=UserRole.STUDENT,
        )
        UserProfile.objects.create(user=outsider, student_group=foreign_group)

        student_qs = bookable_sessions_qs(lab_work_id=lab_work.pk, student=student)
        outsider_qs = bookable_sessions_qs(lab_work_id=lab_work.pk, student=outsider)
        assert restricted_session in student_qs
        assert restricted_session not in outsider_qs

    def test_booking_window_does_not_shift_by_time_of_day(self):
        tz = timezone.get_current_timezone()
        now_early = timezone.make_aware(datetime(2026, 7, 1, 14, 0), tz)
        now_after_close = timezone.make_aware(datetime(2026, 7, 1, 15, 1), tz)
        now_after_open = timezone.make_aware(datetime(2026, 7, 1, 22, 1), tz)

        for moment in (now_early, now_after_close, now_after_open):
            min_date, max_date = booking_date_window(moment)
            assert min_date == date(2026, 7, 2)
            assert max_date == date(2026, 7, 15)

        assert is_day_open_for_booking(date(2026, 7, 2), now_early) is True
        assert is_day_open_for_booking(date(2026, 7, 2), now_after_close) is True
        assert is_day_open_for_booking(date(2026, 7, 16), now_after_close) is False
        assert is_day_open_for_booking(date(2026, 7, 16), now_after_open) is False

    def test_restriction_hours_close_next_day_early(self, room):
        tz = timezone.get_current_timezone()
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория дедлайна"
        )
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, restriction_hours_before_base=12
        )
        next_day_start = timezone.make_aware(datetime(2026, 7, 2, 10, 35), tz)
        assert is_before_restriction_deadline(
            next_day_start,
            timezone.make_aware(datetime(2026, 7, 1, 20, 59), tz),
            laboratory_id=laboratory.pk,
        )
        assert not is_before_restriction_deadline(
            next_day_start,
            timezone.make_aware(datetime(2026, 7, 1, 21, 1), tz),
            laboratory_id=laboratory.pk,
        )

    def test_auto_visited_manual_mode_closes_without_status_change(self, student, session, room):
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория без авто-посещения"
        )
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, auto_visited_mode=AutoVisitedMode.MANUAL
        )
        booking = BookingService().create_booking(student, session.pk)
        session.starts_at = timezone.now() - timedelta(hours=2)
        session.ends_at = timezone.now() - timedelta(minutes=5)
        session.save(update_fields=["starts_at", "ends_at"])

        count = mark_visited_for_ended_sessions()

        booking.refresh_from_db()
        session.refresh_from_db()
        assert count == 0
        assert booking.current_status == BookingStatus.BOOKED
        assert session.status == LabSessionStatus.CLOSED

    def test_auto_visited_at_22_00_waits_until_deadline(self, student, session, room):
        tz = timezone.get_current_timezone()
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория 22:00"
        )
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, auto_visited_mode=AutoVisitedMode.AT_22_00
        )
        booking = BookingService().create_booking(student, session.pk)
        session_date = date(2026, 7, 1)
        session.starts_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=10, minute=35)), tz)
        session.ends_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=12, minute=5)), tz)
        session.save(update_fields=["starts_at", "ends_at"])

        before_deadline = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=21, minute=30)), tz)
        with patch("django.utils.timezone.now", return_value=before_deadline):
            count = mark_visited_for_ended_sessions()
        booking.refresh_from_db()
        session.refresh_from_db()
        assert count == 0
        assert booking.current_status == BookingStatus.BOOKED
        assert session.status == LabSessionStatus.OPEN

        after_deadline = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=22, minute=1)), tz)
        with patch("django.utils.timezone.now", return_value=after_deadline):
            count = mark_visited_for_ended_sessions()
        booking.refresh_from_db()
        session.refresh_from_db()
        assert count == 1
        assert booking.current_status == BookingStatus.VISITED
        assert session.status == LabSessionStatus.CLOSED

    def test_auto_visited_next_day_9_00(self, student, session, room):
        tz = timezone.get_current_timezone()
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория 9:00"
        )
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, auto_visited_mode=AutoVisitedMode.AT_09_00_NEXT
        )
        booking = BookingService().create_booking(student, session.pk)
        session_date = date(2026, 7, 1)
        session.starts_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=10, minute=35)), tz)
        session.ends_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=12, minute=5)), tz)
        session.save(update_fields=["starts_at", "ends_at"])

        too_early = timezone.make_aware(datetime.combine(session_date + timedelta(days=1), datetime.min.time().replace(hour=8, minute=59)), tz)
        assert is_ready_for_auto_visited(session, too_early, auto_visited_mode=AutoVisitedMode.AT_09_00_NEXT) is False

        on_time = timezone.make_aware(datetime.combine(session_date + timedelta(days=1), datetime.min.time().replace(hour=9, minute=1)), tz)
        with patch("django.utils.timezone.now", return_value=on_time):
            count = mark_visited_for_ended_sessions()
        booking.refresh_from_db()
        assert count == 1
        assert booking.current_status == BookingStatus.VISITED

    def test_auto_visited_recovers_closed_sessions_after_manual(self, student, session, room):
        """После MANUAL-закрытия слота включение авто-режима должно догнать BOOKED."""
        tz = timezone.get_current_timezone()
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория догон CLOSED"
        )
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, auto_visited_mode=AutoVisitedMode.MANUAL
        )
        booking = BookingService().create_booking(student, session.pk)
        session_date = date(2026, 7, 1)
        session.starts_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=10, minute=35)), tz)
        session.ends_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=12, minute=5)), tz)
        session.save(update_fields=["starts_at", "ends_at"])

        after_end = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=13, minute=0)), tz)
        with patch("django.utils.timezone.now", return_value=after_end):
            assert mark_visited_for_ended_sessions() == 0
        session.refresh_from_db()
        booking.refresh_from_db()
        assert session.status == LabSessionStatus.CLOSED
        assert booking.current_status == BookingStatus.BOOKED

        LaboratoryBookingSettings.objects.filter(laboratory=laboratory).update(
            auto_visited_mode=AutoVisitedMode.AT_22_00
        )
        after_deadline = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=22, minute=5)), tz)
        with patch("django.utils.timezone.now", return_value=after_deadline):
            count = mark_visited_for_ended_sessions()
        booking.refresh_from_db()
        assert count == 1
        assert booking.current_status == BookingStatus.VISITED

    def test_auto_visited_uses_lab_work_laboratory_when_room_unlinked(
        self, student, session, room
    ):
        """Если у аудитории нет лаборатории — берём лабораторию ЛР."""
        tz = timezone.get_current_timezone()
        laboratory = Laboratory.objects.create(
            training_center=room.training_center, name="Лаборатория через ЛР"
        )
        room.laboratory = None
        room.save(update_fields=["laboratory"])
        session.lab_work.laboratories.add(laboratory)
        LaboratoryBookingSettings.objects.create(
            laboratory=laboratory, auto_visited_mode=AutoVisitedMode.AT_22_00
        )
        booking = BookingService().create_booking(student, session.pk)
        session_date = date(2026, 7, 1)
        session.starts_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=10, minute=35)), tz)
        session.ends_at = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=12, minute=5)), tz)
        session.save(update_fields=["starts_at", "ends_at"])

        after_deadline = timezone.make_aware(datetime.combine(session_date, datetime.min.time().replace(hour=22, minute=5)), tz)
        with patch("django.utils.timezone.now", return_value=after_deadline):
            count = mark_visited_for_ended_sessions()
        booking.refresh_from_db()
        session.refresh_from_db()
        assert count == 1
        assert booking.current_status == BookingStatus.VISITED
        assert session.status == LabSessionStatus.CLOSED

    def test_weekend_sessions_bookable_only_with_schedule(self, lab_work, room, semester):
        weekday_pair = next_open_weekday_pair(days_ahead=3, hour=10, minute=35)
        weekend_pair = weekday_pair
        while weekend_pair.weekday() != 5:
            weekend_pair += timezone.timedelta(days=1)
        non_pair_weekday = weekday_pair.replace(hour=11, minute=0)

        s1 = LabSession.objects.create(
            lab_work=lab_work, room=room, semester=semester,
            starts_at=weekday_pair, ends_at=weekday_pair + timezone.timedelta(minutes=90),
            capacity=2, status=LabSessionStatus.OPEN,
        )
        s2 = LabSession.objects.create(
            lab_work=lab_work, room=room, semester=semester,
            starts_at=weekend_pair, ends_at=weekend_pair + timezone.timedelta(minutes=90),
            capacity=2, status=LabSessionStatus.OPEN,
        )
        s3 = LabSession.objects.create(
            lab_work=lab_work, room=room, semester=semester,
            starts_at=non_pair_weekday, ends_at=non_pair_weekday + timezone.timedelta(minutes=90),
            capacity=2, status=LabSessionStatus.OPEN,
        )
        s4 = LabSession.objects.create(
            lab_work=lab_work, room=room, semester=semester,
            starts_at=weekend_pair.replace(hour=12, minute=35),
            ends_at=weekend_pair.replace(hour=12, minute=35) + timezone.timedelta(minutes=90),
            capacity=2, status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(s1, lab_work=lab_work)
        create_schedule_entry_for_session(s4, lab_work=lab_work)

        qs = bookable_sessions_qs(lab_work_id=lab_work.pk)
        assert s1 in qs
        assert s2 not in qs
        assert s3 not in qs
        assert s4 in qs

    def test_offset_interval_inside_pair_is_bookable(self):
        offset_start = next_open_weekday_pair(days_ahead=3, hour=11, minute=35)
        assert is_pair_time_for_booking(offset_start) is True

    def test_student_busy_intervals_are_hidden(
        self, student, student_group, session, semester
    ):
        BookingService().create_booking(student, session.pk)
        discipline_two = Discipline.objects.create(
            title="Гидравлика", semester=semester, is_published=True
        )
        student_group.disciplines.add(discipline_two)
        second_lab = create_lab_work(
            discipline_two, number=1, title="ЛР 2", duration_minutes=60, is_published=True
        )
        overlap = LabSession.objects.create(
            lab_work=second_lab,
            room=session.room,
            semester=semester,
            starts_at=session.starts_at,
            ends_at=session.starts_at + timezone.timedelta(minutes=60),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        free_start = next_open_weekday_pair(days_ahead=5, hour=10, minute=35)
        free_slot = LabSession.objects.create(
            lab_work=second_lab,
            room=session.room,
            semester=semester,
            starts_at=free_start,
            ends_at=free_start + timezone.timedelta(minutes=60),
            capacity=2,
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(overlap, lab_work=second_lab)
        create_schedule_entry_for_session(free_slot, lab_work=second_lab)
        qs = bookable_sessions_qs(lab_work_id=second_lab.pk, student=student)
        assert overlap not in qs
        assert free_slot in qs
