"""Скоуп-функции сотрудников и студентов на уровне кверисетов (порт queryset-части v1)."""

import pytest
from django.utils import timezone

from apps.academics.models import Discipline, Semester, StudentGroup
from apps.academics.scope import (
    lab_head_disciplines_qs,
    staff_disciplines_qs,
    staff_lab_works_qs,
    staff_students_qs,
    student_disciplines_qs,
    student_support_training_centers_qs,
)
from apps.bookings.models import Booking, SupportTicket
from apps.bookings.scope import (
    staff_bookings_qs,
    staff_can_modify_room,
    staff_rooms_qs,
    staff_sessions_qs,
    staff_support_tickets_qs,
)
from apps.bookings.services import BookingError, BookingService
from apps.scheduling.models import (
    LabDisciplineBinding,
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserProfile, UserRole

from .conftest import create_lab_work, next_open_weekday_pair, seed_manual_booking


def _bind_discipline(laboratory, discipline):
    return LabDisciplineBinding.objects.create(laboratory=laboratory, discipline=discipline)


def _user(email, role, *, laboratory=None, training_center=None, student_group=None, is_staff=True):
    user = User.objects.create_user(
        email=email, password="pass", first_name="F", last_name="L", role=role, is_staff=is_staff  # nosec B106
    )
    UserProfile.objects.create(
        user=user,
        laboratory=laboratory,
        training_center=training_center,
        student_group=student_group,
    )
    return user


@pytest.fixture
def semester(db):
    return Semester.objects.create(
        name="Scope", start_date="2026-01-01", end_date="2026-12-31", is_active=True
    )


@pytest.fixture
def own_tc(db):
    return TrainingCenter.objects.create(number=11, name="Своя лаборатория")


@pytest.fixture
def foreign_tc(db):
    return TrainingCenter.objects.create(number=12, name="Чужая лаборатория")


@pytest.fixture
def own_lab(own_tc):
    return Laboratory.objects.create(training_center=own_tc, name="Своя лаборатория А")


@pytest.fixture
def foreign_lab(foreign_tc):
    return Laboratory.objects.create(training_center=foreign_tc, name="Чужая лаборатория Б")


@pytest.fixture
def own_discipline(semester, own_lab):
    discipline = Discipline.objects.create(title="Своя дисциплина", semester=semester, is_published=True)
    _bind_discipline(own_lab, discipline)
    return discipline


@pytest.fixture
def foreign_discipline(semester, foreign_lab):
    discipline = Discipline.objects.create(title="Чужая дисциплина", semester=semester, is_published=True)
    _bind_discipline(foreign_lab, discipline)
    return discipline


@pytest.fixture
def own_lab_work(own_discipline, own_lab):
    lab_work = create_lab_work(
        own_discipline, number=1, title="Своя ЛР", duration_minutes=90, is_published=True
    )
    lab_work.laboratories.add(own_lab)
    return lab_work


@pytest.fixture
def foreign_lab_work(foreign_discipline, foreign_lab):
    lab_work = create_lab_work(
        foreign_discipline, number=1, title="Чужая ЛР", duration_minutes=90, is_published=True
    )
    lab_work.laboratories.add(foreign_lab)
    return lab_work


@pytest.fixture
def student_group(own_discipline):
    group = StudentGroup.objects.create(name="SCOPE-24")
    group.disciplines.add(own_discipline)
    return group


@pytest.fixture
def foreign_group(foreign_discipline):
    group = StudentGroup.objects.create(name="FOREIGN-SCOPE-24")
    group.disciplines.add(foreign_discipline)
    return group


@pytest.fixture
def student(student_group):
    return _user(
        "scope-student@stud.spmi.ru", UserRole.STUDENT, student_group=student_group, is_staff=False
    )


@pytest.fixture
def staff_with_lab(own_lab, own_tc):
    return _user(
        "scope-staff@spmi.ru", UserRole.LAB_ADMIN, laboratory=own_lab, training_center=own_tc
    )


@pytest.fixture
def staff_no_lab(db):
    return _user("scope-nolab@spmi.ru", UserRole.LAB_ADMIN)


@pytest.fixture
def lab_head(own_lab, own_tc):
    return _user(
        "scope-labhead@spmi.ru", UserRole.LAB_HEAD, laboratory=own_lab, training_center=own_tc
    )


@pytest.fixture
def sys_admin(db):
    return _user("scope-sysadmin@spmi.ru", UserRole.SYS_ADMIN)


@pytest.fixture
def own_room(own_tc, own_lab):
    return Room.objects.create(training_center=own_tc, number="110", capacity=5, laboratory=own_lab)


@pytest.fixture
def foreign_room(foreign_tc, foreign_lab):
    return Room.objects.create(
        training_center=foreign_tc, number="220", capacity=5, laboratory=foreign_lab
    )


@pytest.fixture
def own_session(own_lab_work, own_room, semester):
    starts = next_open_weekday_pair(days_ahead=7)
    return LabSession.objects.create(
        lab_work=own_lab_work,
        room=own_room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        status=LabSessionStatus.OPEN,
    )


@pytest.fixture
def foreign_session(foreign_lab_work, foreign_room, semester):
    starts = next_open_weekday_pair(days_ahead=8)
    return LabSession.objects.create(
        lab_work=foreign_lab_work,
        room=foreign_room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        status=LabSessionStatus.OPEN,
    )


@pytest.fixture
def own_booking(staff_with_lab, student, own_session, own_room):
    own_room.default_lab_staff = staff_with_lab
    own_room.save(update_fields=["default_lab_staff"])
    return BookingService(actor=staff_with_lab).create_booking(
        student, own_session.pk, manual=True
    )


@pytest.fixture
def foreign_booking(staff_with_lab, student, foreign_session):
    return seed_manual_booking(actor=staff_with_lab, student=student, session=foreign_session)


@pytest.fixture
def own_ticket(student, own_tc):
    return SupportTicket.objects.create(
        student=student, subject="Своё", body="Текст", training_center=own_tc
    )


@pytest.fixture
def foreign_ticket(student, foreign_tc):
    return SupportTicket.objects.create(
        student=student, subject="Чужое", body="Текст", training_center=foreign_tc
    )


@pytest.mark.django_db
class TestStaffScopeQuerysets:
    def test_staff_without_lab_sees_nothing(self, staff_no_lab, own_discipline, foreign_discipline):
        assert staff_disciplines_qs(staff_no_lab).count() == 0
        assert lab_head_disciplines_qs(staff_no_lab).count() == 0
        assert staff_bookings_qs(staff_no_lab).count() == 0
        assert staff_support_tickets_qs(staff_no_lab).count() == 0

    def test_staff_disciplines_scoped(self, staff_with_lab, own_discipline, foreign_discipline):
        ids = set(staff_disciplines_qs(staff_with_lab).values_list("pk", flat=True))
        assert own_discipline.pk in ids
        assert foreign_discipline.pk not in ids

    def test_lab_works_scoped(self, staff_with_lab, own_lab_work, foreign_lab_work):
        ids = set(staff_lab_works_qs(staff_with_lab).values_list("pk", flat=True))
        assert own_lab_work.pk in ids
        assert foreign_lab_work.pk not in ids

    def test_lab_head_has_staff_scope(self, lab_head, own_discipline, foreign_discipline):
        ids = set(staff_disciplines_qs(lab_head).values_list("pk", flat=True))
        assert own_discipline.pk in ids
        assert foreign_discipline.pk not in ids

    def test_sys_admin_sees_all(self, sys_admin, own_discipline, foreign_discipline,
                                own_booking, foreign_booking, own_ticket, foreign_ticket):
        assert set(staff_disciplines_qs(sys_admin).values_list("pk", flat=True)) == {
            own_discipline.pk, foreign_discipline.pk
        }
        assert staff_bookings_qs(sys_admin).count() == 2
        assert staff_support_tickets_qs(sys_admin).count() == 2

    def test_bookings_scoped(self, staff_with_lab, own_booking, foreign_booking):
        ids = set(staff_bookings_qs(staff_with_lab).values_list("pk", flat=True))
        assert own_booking.pk in ids
        assert foreign_booking.pk not in ids

    def test_booking_visible_when_room_laboratory_unassigned(
        self, staff_with_lab, student, own_discipline, own_tc, semester
    ):
        """Комната без лаборатории в своём УЦ — записи видимы (толерантность v1)."""
        lab_work = create_lab_work(
            own_discipline, number=2, title="ЛР без лабы", duration_minutes=90, is_published=True
        )
        unassigned_room = Room.objects.create(training_center=own_tc, number="119", capacity=5)
        starts = next_open_weekday_pair(days_ahead=10)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=unassigned_room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        booking = seed_manual_booking(actor=staff_with_lab, student=student, session=session)
        assert booking.pk in set(staff_bookings_qs(staff_with_lab).values_list("pk", flat=True))

    def test_support_tickets_scoped(self, staff_with_lab, own_ticket, foreign_ticket):
        ids = set(staff_support_tickets_qs(staff_with_lab).values_list("pk", flat=True))
        assert own_ticket.pk in ids
        assert foreign_ticket.pk not in ids

    def test_students_scoped_by_group_plan(
        self, staff_with_lab, student, foreign_group
    ):
        foreign_student = _user(
            "foreign-scope-s@stud.spmi.ru", UserRole.STUDENT, is_staff=False
        )
        UserProfile.objects.filter(user=foreign_student).update(student_group=foreign_group)
        ids = set(staff_students_qs(staff_with_lab).values_list("pk", flat=True))
        assert student.pk in ids
        assert foreign_student.pk not in ids

    def test_students_include_those_with_scoped_bookings_only(
        self, staff_with_lab, foreign_group, own_session
    ):
        """Студент чужой группы, но с записью в зоне доступа — виден (ветка записей)."""
        visitor = _user("visitor@stud.spmi.ru", UserRole.STUDENT, is_staff=False)
        UserProfile.objects.filter(user=visitor).update(student_group=foreign_group)
        seed_manual_booking(actor=staff_with_lab, student=visitor, session=own_session)
        ids = set(staff_students_qs(staff_with_lab).values_list("pk", flat=True))
        assert visitor.pk in ids

    def test_students_hide_foreign_without_bookings(self, staff_with_lab, foreign_group):
        visitor = _user("loner@stud.spmi.ru", UserRole.STUDENT, is_staff=False)
        UserProfile.objects.filter(user=visitor).update(student_group=foreign_group)
        ids = set(staff_students_qs(staff_with_lab).values_list("pk", flat=True))
        assert visitor.pk not in ids


@pytest.mark.django_db
class TestStudentScopeQuerysets:
    def test_disciplines_limited_to_group(self, student, own_discipline, foreign_discipline):
        ids = set(student_disciplines_qs(student).values_list("pk", flat=True))
        assert own_discipline.pk in ids
        assert foreign_discipline.pk not in ids

    def test_support_training_centers_limited(self, student, own_tc, foreign_tc):
        ids = set(student_support_training_centers_qs(student).values_list("pk", flat=True))
        assert own_tc.pk in ids
        assert foreign_tc.pk not in ids

    def test_support_training_centers_include_lab_work_owners(self, student, own_lab_work):
        """УЦ выводится и через ЛР (LabWork.training_centers), даже без привязки дисциплины."""
        tc = TrainingCenter.objects.create(number=77)
        own_lab_work.training_centers.add(tc)
        ids = set(student_support_training_centers_qs(student).values_list("pk", flat=True))
        assert tc.pk in ids


@pytest.mark.django_db
class TestBookingVisibilityIntegration:
    def test_booking_scope_matches_session_room(self, staff_with_lab, student, own_session, own_room):
        own_room.default_lab_staff = staff_with_lab
        own_room.save(update_fields=["default_lab_staff"])
        booking = BookingService(actor=staff_with_lab).create_booking(
            student, own_session.pk, manual=True
        )
        assert staff_bookings_qs(staff_with_lab).filter(pk=booking.pk).exists()
        assert Booking.objects.filter(pk=booking.pk).exists()



@pytest.mark.django_db
class TestStaffWriteScope:
    def test_room_scope_by_role(
        self, staff_with_lab, lab_head, own_room, foreign_room
    ):
        own_room.default_lab_staff = staff_with_lab
        own_room.save(update_fields=["default_lab_staff"])

        assert staff_rooms_qs(staff_with_lab).filter(pk=own_room.pk).exists()
        assert not staff_rooms_qs(staff_with_lab).filter(pk=foreign_room.pk).exists()
        assert staff_can_modify_room(staff_with_lab, own_room)
        assert not staff_can_modify_room(staff_with_lab, foreign_room)

        assert staff_can_modify_room(lab_head, own_room)
        assert not staff_can_modify_room(lab_head, foreign_room)

        teacher = _user("scope-teacher@spmi.ru", UserRole.TEACHER)
        assert not staff_can_modify_room(teacher, own_room)

    def test_staff_cannot_modify_foreign_room(
        self, staff_with_lab, foreign_group, foreign_session
    ):
        foreign_student = _user(
            "foreign-write@stud.spmi.ru", UserRole.STUDENT, student_group=foreign_group, is_staff=False
        )
        booking = seed_manual_booking(
            actor=staff_with_lab, student=foreign_student, session=foreign_session
        )

        with pytest.raises(BookingError, match="Нет прав"):
            BookingService(actor=staff_with_lab).change_status(
                booking, "VISITED", note="Посещено"
            )
        with pytest.raises(BookingError, match="Нет прав"):
            BookingService(actor=staff_with_lab).create_booking(
                foreign_student, foreign_session.pk, manual=True
            )

    def test_teacher_sees_only_own_sessions_and_bookings(
        self, own_lab_work, own_room, semester, student
    ):
        teacher_one = _user("scope-teacher-1@spmi.ru", UserRole.TEACHER)
        teacher_two = _user("scope-teacher-2@spmi.ru", UserRole.TEACHER)
        other_student = _user(
            "scope-other-student@stud.spmi.ru", UserRole.STUDENT, is_staff=False
        )
        starts_one = next_open_weekday_pair(days_ahead=7)
        starts_two = next_open_weekday_pair(days_ahead=8)
        session_one = LabSession.objects.create(
            lab_work=own_lab_work,
            room=own_room,
            semester=semester,
            teacher=teacher_one,
            starts_at=starts_one,
            ends_at=starts_one + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        session_two = LabSession.objects.create(
            lab_work=own_lab_work,
            room=own_room,
            semester=semester,
            teacher=teacher_two,
            starts_at=starts_two,
            ends_at=starts_two + timezone.timedelta(minutes=90),
            status=LabSessionStatus.OPEN,
        )
        booking_one = seed_manual_booking(
            actor=teacher_one, student=student, session=session_one
        )
        seed_manual_booking(actor=teacher_two, student=other_student, session=session_two)

        assert set(staff_sessions_qs(teacher_one).values_list("pk", flat=True)) == {
            session_one.pk
        }
        assert set(staff_bookings_qs(teacher_one).values_list("pk", flat=True)) == {
            booking_one.pk
        }

    def test_teacher_export_contains_only_own_rows(
        self, own_lab_work, own_room, semester, student
    ):
        from io import BytesIO

        from openpyxl import load_workbook

        from apps.bookings.reports import generate_report

        teacher_one = _user("scope-export-1@spmi.ru", UserRole.TEACHER)
        teacher_two = _user("scope-export-2@spmi.ru", UserRole.TEACHER)
        other_student = _user(
            "scope-export-student@stud.spmi.ru", UserRole.STUDENT, is_staff=False
        )
        starts_one = next_open_weekday_pair(days_ahead=7)
        starts_two = next_open_weekday_pair(days_ahead=8)
        for teacher, starts, who in (
            (teacher_one, starts_one, student),
            (teacher_two, starts_two, other_student),
        ):
            session = LabSession.objects.create(
                lab_work=own_lab_work,
                room=own_room,
                semester=semester,
                teacher=teacher,
                starts_at=starts,
                ends_at=starts + timezone.timedelta(minutes=90),
                status=LabSessionStatus.OPEN,
            )
            seed_manual_booking(actor=teacher, student=who, session=session)

        payload = generate_report("bookings", staff_user=teacher_one)
        rows = list(load_workbook(BytesIO(payload))["Записи"].iter_rows(values_only=True))
        assert len(rows) == 2
        assert rows[1][1] == student.email
