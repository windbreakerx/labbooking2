"""Стафф-UI (день 7): таблица броней, фильтры/сортировка, статусы, ручная запись.

Скоуп и правила — сервисы (staff_bookings_qs, staff_filters, BookingService);
web-тесты проверяют доступ, отображение, скоуп-изоляцию и обработку ошибок.
"""

import pytest
from django.utils import timezone

from apps.academics.models import Discipline
from apps.bookings.models import Booking, BookingStatus, CancelSource, RegistrationType
from apps.scheduling.models import (
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserRole
from tests.conftest import (
    create_lab_work,
    make_student,
    next_open_weekday_pair,
    seed_manual_booking,
)

BOOKINGS_URL = "/staff/bookings/"
MANUAL_URL = "/staff/manual-booking/"
STUDENTS_PARTIAL_URL = "/staff/manual-booking/students/"
LAB_WORKS_PARTIAL_URL = "/staff/manual-booking/lab-works/"
SLOTS_PARTIAL_URL = "/staff/manual-booking/slots/"


def _login(client, user):
    client.force_login(user)
    return client


def _bind(room, discipline, lab_work):
    """Скоуп стаффа: дисциплина и ЛР привязаны к его лаборатории."""
    discipline.laboratories.add(room.laboratory)
    lab_work.laboratories.add(room.laboratory)


def _teacher(db):
    return User.objects.create_user(
        email="teacher@spmi.ru", password="pass", first_name="У", last_name="Читель",
        role=UserRole.TEACHER,
    )


def _foreign_lab(semester):
    tc2 = TrainingCenter.objects.create(number=9002, name="Чужой УЦ")
    lab2 = Laboratory.objects.create(training_center=tc2, name="Чужая лаборатория")
    room2 = Room.objects.create(training_center=tc2, number="202", capacity=2, laboratory=lab2)
    discipline2 = Discipline.objects.create(
        title="Чужая дисциплина", semester=semester, is_published=True
    )
    lab_work2 = create_lab_work(
        discipline2, number=9, title="Чужая ЛР", duration_minutes=90, is_published=True
    )
    starts = next_open_weekday_pair(days_ahead=3)
    session2 = LabSession.objects.create(
        lab_work=lab_work2,
        room=room2,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        capacity=2,
        status=LabSessionStatus.OPEN,
    )
    return room2, lab_work2, session2


@pytest.fixture
def staff_client(client, staff):
    return _login(client, staff)


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_sent_to_login(self, client):
        response = client.get(BOOKINGS_URL, follow=True)
        assert "Запись на лабораторные" in response.content.decode()

    def test_students_denied(self, client, student):
        response = _login(client, student).get(BOOKINGS_URL, follow=True)
        assert "Доступ только для сотрудников лаборатории." in response.content.decode()

    def test_teacher_read_only(self, client, db):
        response = _login(client, _teacher(db)).get(BOOKINGS_URL)
        page = response.content.decode()
        assert response.status_code == 200
        assert "Ручная запись" not in page
        assert "row-actions" not in page

    def test_teacher_cannot_change_status(self, client, db, staff, student, session):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        response = _login(client, _teacher(db)).post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.VISITED, "note": "х"},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.BOOKED
        assert "Недостаточно прав" in response.content.decode()


@pytest.mark.django_db
class TestBookingsTable:
    def test_shows_scoped_booking(self, staff_client, staff, student, session):
        seed_manual_booking(actor=staff, student=student, session=session)
        page = staff_client.get(BOOKINGS_URL).content.decode()
        assert student.email in page
        assert session.lab_work.title in page
        assert 'href="?sort=student' in page

    def test_hides_foreign_lab_booking(self, staff_client, staff, student, semester, session):
        room2, _lw2, session2 = _foreign_lab(semester)
        seed_manual_booking(actor=staff, student=student, session=session2)
        page = staff_client.get(BOOKINGS_URL).content.decode()
        assert student.email not in page

    def test_status_filter(self, staff_client, staff, student, session, student_group):
        booked = seed_manual_booking(actor=staff, student=student, session=session)
        other = make_student("bob@stud.spmi.ru", student_group)
        visited = seed_manual_booking(actor=staff, student=other, session=session)
        visited.current_status = BookingStatus.VISITED
        visited.save(update_fields=["current_status"])
        page = staff_client.get(BOOKINGS_URL, {"status": BookingStatus.BOOKED}).content.decode()
        assert student.email in page
        assert other.email not in page
        assert booked.pk is not None

    def test_date_window_filter(self, staff_client, staff, student, session):
        seed_manual_booking(actor=staff, student=student, session=session)
        today = timezone.localdate()
        page = staff_client.get(BOOKINGS_URL, {"date_from": today}).content.decode()
        assert student.email in page
        page = staff_client.get(BOOKINGS_URL, {"date_to": today}).content.decode()
        assert student.email not in page

    def test_student_search_filter(self, staff_client, staff, student, session):
        seed_manual_booking(actor=staff, student=student, session=session)
        page = staff_client.get(BOOKINGS_URL, {"student": "stud.spmi"}).content.decode()
        assert student.email in page
        page = staff_client.get(BOOKINGS_URL, {"student": "никого"}).content.decode()
        assert student.email not in page

    def test_sort_by_student(self, staff_client, staff, session, student_group):
        ann = make_student("ann@stud.spmi.ru", student_group)
        bob = make_student("bob@stud.spmi.ru", student_group)
        seed_manual_booking(actor=staff, student=bob, session=session)
        seed_manual_booking(actor=staff, student=ann, session=session)
        page = staff_client.get(BOOKINGS_URL, {"sort": "student", "dir": "asc"}).content.decode()
        assert page.index("Ann") < page.index("Bob")


@pytest.mark.django_db
class TestStatusChange:
    def test_visited_with_note(self, staff_client, staff, student, session):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        response = staff_client.post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.VISITED, "note": "Работа принята"},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.VISITED
        assert "Посетил" in response.content.decode()

    def test_visited_without_note_rejected(self, staff_client, staff, student, session):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        response = staff_client.post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.VISITED, "note": ""},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.BOOKED
        assert "Укажите причину изменения статуса." in response.content.decode()

    def test_no_show_without_note_allowed(self, staff_client, staff, student, session):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        staff_client.post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.NO_SHOW, "note": ""},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.NO_SHOW

    def test_cancel_sets_staff_source(self, staff_client, staff, student, session):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        staff_client.post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.CANCELLED, "note": "По просьбе студента"},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED
        assert booking.cancel_source == CancelSource.STAFF

    def test_foreign_booking_denied(self, staff_client, staff, student, semester):
        _room2, _lw2, session2 = _foreign_lab(semester)
        booking = seed_manual_booking(actor=staff, student=student, session=session2)
        response = staff_client.post(
            f"{BOOKINGS_URL}{booking.pk}/status/",
            {"status": BookingStatus.VISITED, "note": "x"},
            follow=True,
        )
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.BOOKED
        assert "недоступна" in response.content.decode()


@pytest.mark.django_db
class TestManualBooking:
    def test_page_renders(self, staff_client):
        page = staff_client.get(MANUAL_URL).content.decode()
        assert f'hx-get="{STUDENTS_PARTIAL_URL}"' in page
        assert 'id="lab-works"' in page

    def test_student_search_partial(self, staff_client, student):
        page = staff_client.get(STUDENTS_PARTIAL_URL, {"q": "stud"}).content.decode()
        assert f'data-id="{student.pk}"' in page
        assert "<!DOCTYPE" not in page
        page = staff_client.get(STUDENTS_PARTIAL_URL, {"q": "s"}).content.decode()
        assert "data-id" not in page

    def test_lab_works_partial_scoped_to_group_plan(
        self, staff_client, staff, student, session, room, discipline
    ):
        _bind(room, discipline, session.lab_work)
        page = staff_client.get(
            LAB_WORKS_PARTIAL_URL, {"student_id": student.pk}
        ).content.decode()
        assert session.lab_work.title in page
        assert "<!DOCTYPE" not in page

    def test_lab_works_partial_denies_foreign_student(self, staff_client, student, session, room, discipline):
        _bind(room, discipline, session.lab_work)
        outsider = User.objects.create_user(
            email="free@stud.spmi.ru", password="pass", role=UserRole.STUDENT
        )
        response = staff_client.get(LAB_WORKS_PARTIAL_URL, {"student_id": outsider.pk})
        assert response.status_code == 403

    def test_slots_partial(self, staff_client, staff, session, room, discipline):
        _bind(room, discipline, session.lab_work)
        page = staff_client.get(
            SLOTS_PARTIAL_URL, {"lab_work_id": session.lab_work_id}
        ).content.decode()
        assert f'value="{session.pk}"' in page
        assert "10:35" in page

    def test_slots_partial_denies_foreign_lab_work(self, staff_client, semester):
        _room2, lab_work2, _session2 = _foreign_lab(semester)
        response = staff_client.get(SLOTS_PARTIAL_URL, {"lab_work_id": lab_work2.pk})
        assert response.status_code == 403

    def test_post_creates_manual_booking(self, staff_client, staff, student, session, room, discipline):
        _bind(room, discipline, session.lab_work)
        response = staff_client.post(
            MANUAL_URL, {"student_id": student.pk, "session_id": session.pk}, follow=True
        )
        booking = Booking.objects.get(student=student, lab_session=session)
        assert booking.current_status == BookingStatus.BOOKED
        assert booking.registration_type == RegistrationType.MANUAL
        assert booking.registered_by == staff
        assert "записан вручную" in response.content.decode()

    def test_post_warns_when_over_capacity(
        self, staff_client, staff, session, student, student_group, room, discipline
    ):
        _bind(room, discipline, session.lab_work)
        second = make_student("ann@stud.spmi.ru", student_group)
        third = make_student("bob@stud.spmi.ru", student_group)
        seed_manual_booking(actor=staff, student=student, session=session)
        seed_manual_booking(actor=staff, student=second, session=session)
        response = staff_client.post(
            MANUAL_URL, {"student_id": third.pk, "session_id": session.pk}, follow=True
        )
        assert Booking.objects.filter(student=third, lab_session=session).exists()
        assert "Слот заполнен" in response.content.decode()

    def test_post_rejects_foreign_lab_work(self, staff_client, student, semester):
        _room2, lab_work2, session2 = _foreign_lab(semester)
        response = staff_client.post(
            MANUAL_URL,
            {"student_id": student.pk, "session_id": session2.pk},
            follow=True,
        )
        assert not Booking.objects.filter(student=student, lab_session=session2).exists()
        assert "Лабораторная работа недоступна" in response.content.decode()

    def test_post_validates_student_and_session(self, staff_client, student, session):
        response = staff_client.post(MANUAL_URL, {"student_id": 999999}, follow=True)
        assert "Выберите студента" in response.content.decode()
        response = staff_client.post(
            MANUAL_URL, {"student_id": student.pk, "session_id": ""}, follow=True
        )
        assert "Выберите слот" in response.content.decode()
