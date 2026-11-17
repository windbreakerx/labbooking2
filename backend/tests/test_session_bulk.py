"""Тесты массовых операций со слотами (session_bulk)."""

from datetime import timedelta

import pytest

from apps.scheduling.models import Holiday, LabSession, LabSessionStatus
from apps.scheduling.services.session_admin import create_session
from apps.scheduling.services.session_bulk import (
    cancel_day,
    cancel_series,
    copy_day,
    create_series,
)
from tests.conftest import next_open_weekday_pair


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
        # Запрос отмены из исходной лаборатории не должен затронуть чужую аудиторию
        result = cancel_series(
            room.laboratory, semester, room=room, from_date=day, to_date=day, actor=staff, force=True
        )
        assert result["sessions"] == 0
