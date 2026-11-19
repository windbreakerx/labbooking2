"""Правила групповых слотов, половин группы, аудиторий и преподавателей."""

import pytest

from apps.academics.models import Department, Discipline, StudentGroup
from apps.bookings.models import AuditLog
from apps.scheduling.models import (
    LabSession,
    LabSessionBookingMode,
    LabSessionGroupPart,
    Room,
)
from apps.scheduling.services.session_admin import SessionError, create_session
from apps.scheduling.services.session_bulk import create_series
from apps.users.models import User, UserProfile, UserRole

from .conftest import make_student, next_open_weekday_pair


def _payload(lab_work, room, semester, **overrides):
    starts = next_open_weekday_pair(days_ahead=3)
    return {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "date": starts.date().isoformat(),
        "time": starts.time().strftime("%H:%M"),
        **overrides,
    }


def _group_payload(lab_work, room, semester, group, part=LabSessionGroupPart.ALL):
    return _payload(
        lab_work,
        room,
        semester,
        booking_mode=LabSessionBookingMode.GROUP,
        occupant_group_id=group.pk,
        group_part=part,
    )


def _teacher(email, role=UserRole.TEACHER, **profile):
    user = User.objects.create_user(
        email=email,
        password="pass",  # nosec B106
        first_name="Т",
        last_name=email.split("@")[0],
        role=role,
    )
    UserProfile.objects.create(user=user, **profile)
    return user


@pytest.mark.django_db
class TestGroupScope:
    def test_group_without_discipline_rejected(
        self, lab_work, room, semester, staff
    ):
        other_discipline = Discipline.objects.create(
            title="Чужая дисциплина", semester=semester
        )
        foreign_group = StudentGroup.objects.create(name="FOREIGN-1")
        foreign_group.disciplines.add(other_discipline)

        with pytest.raises(SessionError, match="не изучает"):
            create_session(
                room.laboratory,
                _group_payload(lab_work, room, semester, foreign_group),
                actor=staff,
            )

    def test_group_with_discipline_but_without_lab_work_allowed(
        self, lab_work, room, semester, staff, student_group
    ):
        make_student("plan@stud.spmi.ru", student_group)
        session = create_session(
            room.laboratory,
            _group_payload(lab_work, room, semester, student_group),
            actor=staff,
        )
        assert session.occupant_group == student_group

    def test_group_with_lab_work_in_plan_allowed(
        self, lab_work, room, semester, staff, student_group
    ):
        make_student("plan2@stud.spmi.ru", student_group)
        student_group.lab_works.add(lab_work)
        session = create_session(
            room.laboratory,
            _group_payload(lab_work, room, semester, student_group),
            actor=staff,
        )
        assert session.occupant_group == student_group


@pytest.mark.django_db
class TestGroupPart:
    def test_half_group_books_only_half(self, lab_work, room, semester, staff, student_group):
        room.capacity = 10
        room.save(update_fields=["capacity"])
        for index in range(4):
            make_student(f"half{index}@stud.spmi.ru", student_group)

        session = create_session(
            room.laboratory,
            _group_payload(
                lab_work, room, semester, student_group, LabSessionGroupPart.FIRST
            ),
            actor=staff,
        )

        assert session.group_part == LabSessionGroupPart.FIRST
        assert session.capacity == 2
        assert session.bookings.count() == 2

    def test_group_over_room_capacity_allowed_and_audited(
        self, lab_work, room, semester, staff, student_group
    ):
        room.capacity = 1
        room.save(update_fields=["capacity"])
        make_student("one@stud.spmi.ru", student_group)
        make_student("two@stud.spmi.ru", student_group)

        session = create_session(
            room.laboratory,
            _group_payload(lab_work, room, semester, student_group),
            actor=staff,
        )

        assert session.capacity == 2
        assert AuditLog.objects.filter(
            action="session.group_overflow", entity_id=session.pk
        ).exists()

    def test_group_bookings_write_audit(
        self, lab_work, room, semester, staff, student_group
    ):
        make_student("audit@stud.spmi.ru", student_group)
        session = create_session(
            room.laboratory,
            _group_payload(lab_work, room, semester, student_group),
            actor=staff,
        )
        assert AuditLog.objects.filter(
            action="booking.group_create", entity_id=session.pk
        ).exists()

    def test_series_keeps_group_and_part(
        self, lab_work, room, semester, staff, student_group
    ):
        room.capacity = 10
        room.save(update_fields=["capacity"])
        for index in range(4):
            make_student(f"series{index}@stud.spmi.ru", student_group)
        starts = next_open_weekday_pair(days_ahead=3)

        result = create_series(
            room.laboratory,
            {
                **_group_payload(
                    lab_work,
                    room,
                    semester,
                    student_group,
                    LabSessionGroupPart.SECOND,
                ),
                "start_date": starts.date().isoformat(),
                "start_time": starts.time().strftime("%H:%M"),
                "repeat": "NONE",
                "occurrences": 1,
            },
            actor=staff,
        )

        assert result["created"] == 1
        session = LabSession.objects.get(lab_work=lab_work)
        assert session.group_part == LabSessionGroupPart.SECOND
        assert session.capacity == 2


@pytest.mark.django_db
class TestRoomAndTeacherScope:
    def test_unassigned_room_rejected(self, lab_work, room, semester, staff):
        shared_room = Room.objects.create(
            training_center=room.training_center, number="999", capacity=10
        )
        with pytest.raises(SessionError, match="не закреплена"):
            create_session(
                room.laboratory,
                _payload(lab_work, shared_room, semester),
                actor=staff,
            )

    def test_teacher_department_must_match_lab_work(
        self, lab_work, room, semester, staff
    ):
        own_department = Department.objects.create(title="Кафедра своей ЛР")
        foreign_department = Department.objects.create(title="Чужая кафедра")
        lab_work.disciplines.update(department=own_department)
        own_teacher = _teacher("own@spmi.ru", department=own_department)
        foreign_teacher = _teacher("foreign@spmi.ru", department=foreign_department)

        session = create_session(
            room.laboratory,
            _payload(lab_work, room, semester, teacher_id=own_teacher.pk),
            actor=staff,
        )
        assert session.teacher == own_teacher

        starts = next_open_weekday_pair(days_ahead=4)
        with pytest.raises(SessionError, match="не относится к кафедре"):
            create_session(
                room.laboratory,
                {
                    **_payload(lab_work, room, semester, teacher_id=foreign_teacher.pk),
                    "date": starts.date().isoformat(),
                    "time": starts.time().strftime("%H:%M"),
                },
                actor=staff,
            )

    def test_teacher_sees_department_lab_works_only(
        self, lab_work, room, semester, staff
    ):
        from apps.academics.scope import staff_lab_works_qs
        from apps.bookings.scope import staff_sessions_qs

        own_department = Department.objects.create(title="Кафедра видимости")
        foreign_department = Department.objects.create(title="Другая кафедра")
        lab_work.disciplines.update(department=own_department)
        teacher = _teacher("scope@spmi.ru", department=own_department)

        foreign_discipline = Discipline.objects.create(
            title="Чужая", semester=semester, department=foreign_department
        )
        foreign_work = lab_work.__class__.objects.create(
            number=2, title="Чужая ЛР", duration_minutes=90, is_published=True
        )
        foreign_work.disciplines.add(foreign_discipline)
        foreign_work.laboratories.add(room.laboratory)

        session = create_session(
            room.laboratory,
            _payload(lab_work, room, semester, teacher_id=teacher.pk),
            actor=staff,
        )

        assert staff_lab_works_qs(teacher).filter(pk=lab_work.pk).exists()
        assert not staff_lab_works_qs(teacher).filter(pk=foreign_work.pk).exists()
        assert staff_sessions_qs(teacher).filter(pk=session.pk).exists()
        assert not staff_sessions_qs(teacher).filter(lab_work=foreign_work).exists()
