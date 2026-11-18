"""Тесты новых отчётов фазы 5: загруженность аудиторий/преподавателей, учебный план."""


import pytest
from django.utils import timezone

from apps.academics.models import GroupDisciplineLoad
from apps.bookings.models import BookingStatus
from apps.bookings.reports import (
    curriculum_rows,
    dashboard_kpi,
    room_load_rows,
    teacher_load_rows,
)
from apps.bookings.services import BookingService
from apps.scheduling.models import LabSession, LabSessionStatus
from apps.users.models import UserRole
from tests.conftest import create_schedule_entry_for_session, next_open_weekday_pair


@pytest.fixture
def teacher_user(db):
    from apps.users.models import User

    return User.objects.create_user(
        email="teacher@spmi.ru",
        password="pass",  # nosec B106
        first_name="У",
        last_name="Читель",
        role=UserRole.TEACHER,
    )


@pytest.mark.django_db
class TestPhase5Reports:
    def test_room_load_report(self, lab_work, room, semester, staff, student):
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
        BookingService(actor=student).create_booking(student, session.pk)

        rows = room_load_rows(staff)
        assert len(rows) == 1
        row = rows[0]
        assert row["room_number"] == "101"
        assert row["sessions"] == 1
        assert row["bookings"] == 1
        assert row["utilization"] == 50.0

    def test_teacher_load_report(self, lab_work, room, semester, staff, student, teacher_user):
        starts = next_open_weekday_pair(days_ahead=3)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            teacher=teacher_user,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        create_schedule_entry_for_session(session)
        BookingService(actor=student).create_booking(student, session.pk)

        rows = teacher_load_rows(staff)
        assert len(rows) == 1
        assert rows[0]["teacher_name"] == "У Читель"
        assert rows[0]["bookings"] == 1

    def test_teacher_sees_only_self(self, lab_work, room, semester, student, teacher_user):
        from apps.users.models import User

        other_teacher = User.objects.create_user(
            email="other@spmi.ru", password="pass", role=UserRole.TEACHER  # nosec B106
        )
        starts1 = next_open_weekday_pair(days_ahead=3)
        starts2 = next_open_weekday_pair(days_ahead=6)
        for t, starts in ((teacher_user, starts1), (other_teacher, starts2)):
            session = LabSession.objects.create(
                lab_work=lab_work,
                room=room,
                semester=semester,
                teacher=t,
                starts_at=starts,
                ends_at=starts + timezone.timedelta(minutes=90),
                status=LabSessionStatus.OPEN,
            )
            create_schedule_entry_for_session(session)

        rows = teacher_load_rows(teacher_user)
        assert len(rows) == 1
        assert rows[0]["teacher"].pk == teacher_user.pk

    def test_curriculum_report_counts_required_sessions(
        self, lab_work, room, semester, discipline, student_group, student, staff
    ):
        lab_work.required_sessions = 2
        lab_work.save(update_fields=["required_sessions"])
        discipline.laboratories.add(room.laboratory)
        GroupDisciplineLoad.objects.create(
            discipline=discipline, group=student_group, semester=semester, is_active=True
        )

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
        BookingService(actor=staff).change_status(
            booking, BookingStatus.VISITED, note="Посещено"
        )

        rows = curriculum_rows(staff)
        assert len(rows) == 1
        row = rows[0]
        assert row["required_sessions"] == 2
        assert row["required_total"] == 2  # 2 * 1 студент
        assert row["visited"] == 1
        assert row["progress"] == 50.0

    def test_dashboard_kpi(self, lab_work, room, semester, staff, student):
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
        BookingService(actor=student).create_booking(student, session.pk)

        kpi = dashboard_kpi(room.laboratory)
        assert kpi["total_sessions"] == 1
        assert kpi["booked"] == 1
