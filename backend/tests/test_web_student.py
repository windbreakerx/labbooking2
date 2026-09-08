"""Студентческий UI (день 6): каталог, wizard записи, мои записи с отменой.

Слоты и правила — сервисы (student_bookable_slots, BookingService);
web-тесты проверяют скоуп, отображение и обработку ошибок сервисов.
"""

import pytest

from apps.academics.models import (
    Discipline,
    GroupLabWorkOverride,
    GroupLabWorkOverrideMode,
)
from apps.bookings.models import Booking, BookingStatus
from apps.users.models import User
from tests.conftest import create_lab_work, seed_manual_booking

DISCIPLINES_URL = "/disciplines/"
MY_BOOKINGS_URL = "/my-bookings/"


def _login(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def student_client(client, student):
    return _login(client, student)


@pytest.fixture
def extra_lab_work(discipline):
    return create_lab_work(discipline, number=2, title="ЛР 2", duration_minutes=90, is_published=True)


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_sent_to_login(self, client):
        response = client.get(DISCIPLINES_URL, follow=True)
        assert "Запись на лабораторные" in response.content.decode()

    def test_catalog_is_students_only(self, client, staff):
        _login(client, staff)
        response = client.get(DISCIPLINES_URL, follow=True)
        assert "Доступ только для студентов." in response.content.decode()


@pytest.mark.django_db
class TestCatalog:
    def test_shows_group_disciplines(self, student_client, student_group, discipline, lab_work):
        page = student_client.get(DISCIPLINES_URL).content.decode()
        assert discipline.title in page
        assert "Лабораторных работ: 1" in page
        assert f'href="/disciplines/{discipline.pk}/lab-works/"' in page

    def test_hides_foreign_discipline(self, student_client, discipline):
        Discipline.objects.create(title="Чужая дисциплина")
        page = student_client.get(DISCIPLINES_URL).content.decode()
        assert "Чужая дисциплина" not in page

    def test_empty_without_group(self, client, db):
        orphan = User.objects.create_user(
            email="orphan@stud.spmi.ru", password="pass", first_name="Б", last_name="Ез"
        )
        page = _login(client, orphan).get(DISCIPLINES_URL).content.decode()
        assert "Дисциплины не назначены" in page

    def test_lab_works_list(self, student_client, discipline, lab_work):
        page = student_client.get(f"/disciplines/{discipline.pk}/lab-works/").content.decode()
        assert lab_work.title in page
        assert f'href="/lab-works/{lab_work.pk}/book/"' in page

    def test_hidden_lab_work_excluded(self, student_client, discipline, lab_work, semester, student_group):
        GroupLabWorkOverride.objects.create(
            group=student_group, semester=semester, lab_work=lab_work,
            mode=GroupLabWorkOverrideMode.HIDDEN,
        )
        page = student_client.get(f"/disciplines/{discipline.pk}/lab-works/").content.decode()
        assert lab_work.title not in page

    def test_foreign_discipline_denied(self, student_client):
        foreign = Discipline.objects.create(title="Чужая")
        response = student_client.get(f"/disciplines/{foreign.pk}/lab-works/", follow=True)
        assert "Дисциплина недоступна" in response.content.decode()


@pytest.mark.django_db
class TestWizard:
    def test_book_page_shows_slot(self, student_client, lab_work, session):
        page = student_client.get(f"/lab-works/{lab_work.pk}/book/").content.decode()
        assert f'href="/lab-works/{lab_work.pk}/book/{session.pk}/"' in page
        assert "Свободно мест: 2" in page

    def test_book_page_empty_without_slots(self, student_client, extra_lab_work):
        page = student_client.get(f"/lab-works/{extra_lab_work.pk}/book/").content.decode()
        assert "Открытых слотов нет" in page

    def test_confirm_page_shows_summary(self, student_client, lab_work, session):
        page = student_client.get(f"/lab-works/{lab_work.pk}/book/{session.pk}/").content.decode()
        assert "Подтверждение записи" in page
        assert session.room.__str__() in page
        assert "Записаться" in page

    def test_confirm_denied_for_foreign_slot(self, student_client, lab_work, extra_lab_work, session):
        # сессия другой ЛР: в каталоге этой ЛР её нет
        response = student_client.get(
            f"/lab-works/{extra_lab_work.pk}/book/{session.pk}/", follow=True
        )
        assert "Слот недоступен" in response.content.decode()

    def test_post_creates_booking(self, student_client, student, lab_work, session):
        response = student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/")
        booking = Booking.objects.get(student=student, lab_session=session)
        assert response.status_code == 302
        assert response.url == MY_BOOKINGS_URL
        assert booking.current_status == BookingStatus.BOOKED
        page = student_client.get(MY_BOOKINGS_URL).content.decode()
        assert "Вы записаны" in page
        assert lab_work.title in page

    def test_post_denied_when_full(self, client, student, lab_work, session, student_group):
        # оба места заняты другими — слот исчезает из каталога
        for i in range(2):
            guest = User.objects.create_user(
                email=f"guest{i}@stud.spmi.ru", password="pass", first_name="Г", last_name=f"№{i}"
            )
            seed_manual_booking(actor=None, student=guest, session=session)
        _login(client, student)
        page = client.get(f"/lab-works/{lab_work.pk}/book/").content.decode()
        assert "Открытых слотов нет" in page
        response = client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/", follow=True)
        assert "Слот недоступен" in response.content.decode()
        assert not Booking.objects.filter(student=student).exists()

    def test_post_after_own_booking_shows_error(self, student_client, student, lab_work, session):
        student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/")
        response = student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/", follow=True)
        assert "Слот недоступен" in response.content.decode()  # собственная запись перекрывает слот
        assert Booking.objects.filter(student=student).count() == 1

    def test_hidden_lab_work_book_denied(
        self, student_client, lab_work, semester, student_group
    ):
        GroupLabWorkOverride.objects.create(
            group=student_group, semester=semester, lab_work=lab_work,
            mode=GroupLabWorkOverrideMode.HIDDEN,
        )
        response = student_client.get(f"/lab-works/{lab_work.pk}/book/", follow=True)
        assert "недоступна для вашей группы" in response.content.decode()


@pytest.mark.django_db
class TestMyBookings:
    def test_shows_upcoming_and_badge(self, student_client, student, lab_work, session):
        student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/")
        page = student_client.get(MY_BOOKINGS_URL).content.decode()
        assert "badge--booked" in page
        assert "Отменить" in page

    def test_cancel(self, student_client, student, lab_work, session):
        student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/")
        booking = Booking.objects.get(student=student)
        response = student_client.post(f"/my-bookings/{booking.pk}/cancel/", follow=True)
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED
        assert "Запись отменена" in response.content.decode()
        assert "Отменить" not in response.content.decode().split("История")[0]

    def test_cancel_twice_denied(self, student_client, student, lab_work, session):
        student_client.post(f"/lab-works/{lab_work.pk}/book/{session.pk}/")
        booking = Booking.objects.get(student=student)
        student_client.post(f"/my-bookings/{booking.pk}/cancel/")
        response = student_client.post(f"/my-bookings/{booking.pk}/cancel/", follow=True)
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.CANCELLED
        assert "Можно отменить только активную запись" in response.content.decode()

    def test_cancel_foreign_booking_404(self, student_client, staff, lab_work, session, student_group):
        guest = User.objects.create_user(
            email="guest@stud.spmi.ru", password="pass", first_name="Г", last_name="№"
        )
        guest_booking = seed_manual_booking(actor=staff, student=guest, session=session)
        assert student_client.post(f"/my-bookings/{guest_booking.pk}/cancel/").status_code == 404
