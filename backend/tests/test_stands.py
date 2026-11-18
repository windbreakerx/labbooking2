"""Стенды в расписании: комната, параллельные пары, разные ЛР."""

import pytest
from django.utils import timezone

from apps.scheduling.models import LabStand, Room
from apps.scheduling.services.session_admin import SessionError, create_session

from .conftest import (
    create_lab_work,
    make_student,
    next_open_weekday_pair,
    seed_manual_booking,
)


def _payload(lab_work, room, semester, starts_at):
    return {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "date": starts_at.date().isoformat(),
        "time": starts_at.time().strftime("%H:%M"),
    }


def _stand(room, *, number="ST-1", capacity=3):
    return LabStand.objects.create(
        name=f"Стенд {number}",
        inventory_number=number,
        training_center=room.training_center,
        room=room,
        capacity=capacity,
    )


def _second_room(room):
    return Room.objects.create(
        training_center=room.training_center,
        number="102",
        laboratory=room.laboratory,
    )


@pytest.mark.django_db
class TestStandScheduling:
    def test_slot_requires_stand_in_room(self, lab_work, room, semester, staff):
        other_room = _second_room(room)
        lab_work.stands.add(_stand(other_room))
        starts = next_open_weekday_pair(days_ahead=3)

        with pytest.raises(SessionError, match="нет стенда"):
            create_session(
                room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
            )

    def test_lab_work_without_stands_allows_parallel_rooms(
        self, lab_work, room, semester, staff
    ):
        other_room = _second_room(room)
        starts = next_open_weekday_pair(days_ahead=3)

        create_session(
            room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
        )
        second = create_session(
            room.laboratory,
            _payload(lab_work, other_room, semester, starts),
            actor=staff,
        )
        assert second.pk is not None

    def test_one_stand_blocks_parallel_same_lab_in_room(
        self, lab_work, room, semester, staff
    ):
        lab_work.stands.add(_stand(room))
        starts = next_open_weekday_pair(days_ahead=3)

        create_session(
            room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
        )
        with pytest.raises(SessionError, match="заняты"):
            create_session(
                room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
            )

    def test_two_stands_in_one_room_allow_two_parallel_sessions(
        self, discipline, room, semester, staff
    ):
        lab_work = create_lab_work(
            discipline, number=2, title="ЛР на 60 минут", duration_minutes=60, capacity=2
        )
        lab_work.laboratories.add(room.laboratory)
        lab_work.stands.add(_stand(room, number="ST-1"), _stand(room, number="ST-2"))
        starts = next_open_weekday_pair(days_ahead=3)

        create_session(
            room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
        )
        create_session(
            room.laboratory,
            _payload(lab_work, room, semester, starts + timezone.timedelta(minutes=15)),
            actor=staff,
        )
        with pytest.raises(SessionError, match="заняты"):
            create_session(
                room.laboratory,
                _payload(
                    lab_work, room, semester, starts + timezone.timedelta(minutes=30)
                ),
                actor=staff,
            )

    def test_two_stands_in_two_rooms_allow_one_session_each(
        self, lab_work, room, semester, staff
    ):
        other_room = _second_room(room)
        lab_work.stands.add(_stand(room, number="ST-1"), _stand(other_room, number="ST-2"))
        starts = next_open_weekday_pair(days_ahead=3)

        create_session(
            room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
        )
        create_session(
            room.laboratory,
            _payload(lab_work, other_room, semester, starts),
            actor=staff,
        )
        with pytest.raises(SessionError, match="заняты"):
            create_session(
                room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
            )


@pytest.mark.django_db
class TestStandCapacity:
    def test_two_rooms_with_same_stands_are_independent(
        self, lab_work, room, semester, staff, student_group
    ):
        other_room = _second_room(room)
        lab_work.capacity = 3
        lab_work.save(update_fields=["capacity"])
        lab_work.stands.add(_stand(room, number="ST-1"), _stand(other_room, number="ST-2"))
        room.capacity = 10
        room.save(update_fields=["capacity"])
        starts = next_open_weekday_pair(days_ahead=3)

        first = create_session(
            room.laboratory, _payload(lab_work, room, semester, starts), actor=staff
        )
        second = create_session(
            room.laboratory,
            _payload(lab_work, other_room, semester, starts),
            actor=staff,
        )

        for index in range(3):
            student = make_student(f"stand-room-{index}@stud.spmi.ru", student_group)
            seed_manual_booking(actor=staff, student=student, session=first)
            student = make_student(f"stand-room-other-{index}@stud.spmi.ru", student_group)
            seed_manual_booking(actor=staff, student=student, session=second)

        assert first.booked_count == 3
        assert second.booked_count == 3
        assert first.available_seats == 0
        assert second.available_seats == 0
