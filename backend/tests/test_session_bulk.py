"""Тесты массовых операций со слотами (session_bulk)."""

from datetime import timedelta

import pytest

from apps.academics.models import Department
from apps.scheduling.models import Holiday, LabSession, LabSessionStatus, LabStand
from apps.scheduling.services.session_admin import create_session
from apps.scheduling.services.session_bulk import (
    BulkError,
    cancel_day,
    cancel_series,
    copy_day,
    copy_week,
    create_series,
)
from apps.users.models import User, UserProfile, UserRole

from .conftest import (
    create_lab_work,
    make_student,
    next_open_weekday_pair,
    seed_manual_booking,
)


def _series_payload(lab_work, room, semester, start):
    return {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "start_date": start.date().isoformat(),
        "start_time": start.time().strftime("%H:%M"),
        "repeat": "WEEKLY",
        "occurrences": "3",
        "skip_holidays": "true",
    }


@pytest.mark.django_db
class TestSessionBulk:
    def test_create_series_three_weeks(self, lab_work, room, semester, staff):
        start = next_open_weekday_pair(days_ahead=3)
        result = create_series(
            room.laboratory,
            _series_payload(lab_work, room, semester, start),
            actor=staff,
        )
        assert result["created"] == 3
        assert result["skipped_holidays"] == 0
        assert result["skipped_duplicates"] == 0
        sessions = LabSession.objects.filter(lab_work=lab_work, room=room).count()
        assert sessions == 3

    def test_create_series_skips_holidays(self, lab_work, room, semester, staff):
        start = next_open_weekday_pair(days_ahead=3)
        # Вторая неделя попадёт на тот же день недели; создаём праздник на вторую дату
        second_date = (start + timedelta(weeks=1)).date()
        Holiday.objects.create(date=second_date, name="Выходной")
        result = create_series(
            room.laboratory,
            _series_payload(lab_work, room, semester, start),
            actor=staff,
        )
        assert result["created"] == 2
        assert result["skipped_holidays"] == 1

    def test_copy_day(self, lab_work, room, semester, staff):
        source = next_open_weekday_pair(days_ahead=3)
        create_session(
            room.laboratory,
            {
                "lab_work_id": lab_work.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": source.date().isoformat(),
                "time": source.time().strftime("%H:%M"),
            },
            actor=staff,
        )
        target = source + timedelta(days=1)
        result = copy_day(
            room.laboratory, semester, room, source.date(), target.date(), actor=staff
        )
        assert result["created"] == 1
        assert LabSession.objects.filter(room=room, starts_at__date=target.date()).exists()

    def test_cancel_day_preview_and_force(self, lab_work, room, semester, staff, student):
        day = next_open_weekday_pair(days_ahead=3).date()
        session = create_session(
            room.laboratory,
            {
                "lab_work_id": lab_work.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": day.isoformat(),
                "time": "10:35",
            },
            actor=staff,
        )
        from apps.bookings.services import BookingService

        BookingService(actor=student).create_booking(student, session.pk)

        preview = cancel_day(room.laboratory, semester, room, day, actor=staff, force=False)
        assert preview["sessions"] == 1
        assert preview["booked_bookings"] == 1
        session.refresh_from_db()
        assert session.status == LabSessionStatus.OPEN  # пока не отменён

        cancel_day(room.laboratory, semester, room, day, actor=staff, force=True)
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED

    def test_cancel_series_out_of_scope_fails(self, lab_work, room, semester, staff):
        from apps.academics.models import Discipline
        from apps.scheduling.models import Laboratory, Room, TrainingCenter

        other_tc = TrainingCenter.objects.create(number=7777, name="Чужой")
        other_lab = Laboratory.objects.create(training_center=other_tc, name="Чужая")
        other_room = Room.objects.create(
            training_center=other_tc, number="777", capacity=5, laboratory=other_lab
        )
        Discipline.objects.create(
            title="Чужая", semester=semester, is_published=True
        )
        other_work = lab_work
        # Перепривязываем ЛР к чужой лаборатории для проверки скоупа
        other_work.laboratories.add(other_lab)
        day = next_open_weekday_pair(days_ahead=3).date()
        create_session(
            other_lab,
            {
                "lab_work_id": other_work.pk,
                "room_id": other_room.pk,
                "semester_id": semester.pk,
                "date": day.isoformat(),
                "time": "10:35",
            },
            actor=staff,
        )
        # Запрос отмены из исходной лаборатории по чужой аудитории — ошибка, не ноль
        with pytest.raises(BulkError, match="другой лаборатории"):
            cancel_series(
                room.laboratory,
                semester,
                room=other_room,
                from_date=day,
                to_date=day,
                actor=staff,
                force=True,
            )

    def test_series_rolls_back_on_teacher_conflict(self, lab_work, room, semester, staff):
        teacher = User.objects.create_user(
            email="series-teacher@spmi.ru",
            password="pass",
            first_name="Т",
            last_name="Преподаватель",
            role=UserRole.TEACHER,
        )
        department = Department.objects.create(title="Кафедра серии")
        lab_work.disciplines.update(department=department)
        UserProfile.objects.create(user=teacher, department=department)
        start = next_open_weekday_pair(days_ahead=3)
        third = start + timedelta(weeks=2)
        other_lab = create_lab_work(
            lab_work.disciplines.first(),
            number=2,
            title="Другая ЛР",
            duration_minutes=90,
            is_published=True,
        )
        other_lab.laboratories.add(room.laboratory)
        LabSession.objects.create(
            lab_work=other_lab,
            room=room,
            semester=semester,
            teacher=teacher,
            starts_at=third,
            ends_at=third + timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )

        payload = _series_payload(lab_work, room, semester, start)
        payload["teacher_id"] = teacher.pk
        with pytest.raises(BulkError) as exc:
            create_series(room.laboratory, payload, actor=staff)

        assert third.strftime("%d.%m.%Y") in str(exc.value)
        assert not LabSession.objects.filter(lab_work=lab_work).exists()

    def test_cancel_series_preview_counts_bookings(
        self, lab_work, room, semester, staff, student, student_group
    ):
        day = next_open_weekday_pair(days_ahead=3).date()
        session = create_session(
            room.laboratory,
            {
                "lab_work_id": lab_work.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": day.isoformat(),
                "time": "10:35",
            },
            actor=staff,
        )
        other = make_student("bulk-preview-2@stud.spmi.ru", student_group)
        seed_manual_booking(actor=staff, student=student, session=session)
        seed_manual_booking(actor=staff, student=other, session=session)

        preview = cancel_series(
            room.laboratory,
            semester,
            room=room,
            from_date=day,
            to_date=day,
            actor=staff,
            force=False,
        )
        assert preview["sessions"] == 1
        assert preview["booked_bookings"] == 2

    def test_copy_week_requires_monday(self, lab_work, room, semester, staff):
        source = next_open_weekday_pair(days_ahead=3).date()
        while source.weekday() == 0:
            source += timedelta(days=1)
        target = source + timedelta(weeks=1)

        with pytest.raises(BulkError, match="понедельник"):
            copy_week(
                room.laboratory,
                semester,
                room,
                source,
                target,
                actor=staff,
            )

    def test_different_labs_share_room_at_same_time(
        self, lab_work, discipline, room, semester, staff
    ):
        other_lab = create_lab_work(
            discipline,
            number=2,
            title="Вторая ЛР",
            duration_minutes=90,
            is_published=True,
        )
        other_lab.laboratories.add(room.laboratory)
        starts = next_open_weekday_pair(days_ahead=3)

        create_session(
            room.laboratory,
            {
                "lab_work_id": lab_work.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": starts.date().isoformat(),
                "time": starts.strftime("%H:%M"),
            },
            actor=staff,
        )
        second = create_session(
            room.laboratory,
            {
                "lab_work_id": other_lab.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": starts.date().isoformat(),
                "time": starts.strftime("%H:%M"),
            },
            actor=staff,
        )
        assert second.pk is not None

    def test_copy_day_rolls_back_on_stand_conflict(
        self, lab_work, discipline, room, semester, staff
    ):
        stand = LabStand.objects.create(
            name="Общий стенд копирования",
            inventory_number="COPY-ST-1",
            training_center=room.training_center,
            room=room,
        )
        lab_work.stands.add(stand)
        other_lab = create_lab_work(
            discipline,
            number=2,
            title="Вторая ЛР",
            duration_minutes=90,
            is_published=True,
        )
        other_lab.laboratories.add(room.laboratory)
        other_lab.stands.add(stand)

        source_date = next_open_weekday_pair(days_ahead=3)
        target_date = source_date + timedelta(days=1)
        create_session(
            room.laboratory,
            {
                "lab_work_id": lab_work.pk,
                "room_id": room.pk,
                "semester_id": semester.pk,
                "date": source_date.date().isoformat(),
                "time": source_date.strftime("%H:%M"),
            },
            actor=staff,
        )
        LabSession.objects.create(
            lab_work=other_lab,
            room=room,
            semester=semester,
            starts_at=target_date,
            ends_at=target_date + timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )

        with pytest.raises(BulkError) as exc:
            copy_day(
                room.laboratory,
                semester,
                room,
                source_date.date(),
                target_date.date(),
                actor=staff,
            )

        assert target_date.strftime("%d.%m.%Y") in str(exc.value)
        assert not LabSession.objects.filter(
            lab_work=lab_work, starts_at__date=target_date.date()
        ).exists()
