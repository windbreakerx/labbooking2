"""Мессенджер обращений (день 6): доступ, переписка, поллинг-фрагменты,
unread/overdue, статусы. Переписка идёт поверх SupportService."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.bookings.models import SupportMessage, SupportTicket
from apps.scheduling.models import Laboratory, TrainingCenter
from apps.users.models import User, UserProfile, UserRole

SUPPORT_URL = "/support/"
CREATE_URL = "/support/create/"
STAFF_URL = "/staff/support/"


def make_ticket(student, *, tc, subject="Не работает стенд", body="Описание проблемы", created_days_ago=0):
    ticket = SupportTicket.objects.create(
        student=student, subject=subject, body=body, training_center=tc
    )
    if created_days_ago:
        SupportTicket.objects.filter(pk=ticket.pk).update(
            created_at=timezone.now() - timedelta(days=created_days_ago)
        )
    return ticket


@pytest.fixture
def bound_student(student, discipline, room):
    """Студент, чья дисциплина привязана к лаборатории — может писать в её УЦ."""
    discipline.laboratories.add(room.laboratory)
    return student


@pytest.fixture
def other_tc(db):
    tc = TrainingCenter.objects.create(number=9002, name="Чужой УЦ")
    Laboratory.objects.create(training_center=tc, name="Чужая лаборатория")
    return tc


@pytest.fixture
def other_staff(other_tc):
    user = User.objects.create_user(
        email="other-staff@spmi.ru", password="pass", first_name="О", last_name="С",
        role=UserRole.LAB_ADMIN, is_staff=True,
    )
    UserProfile.objects.create(user=user, training_center=other_tc)
    return user


def _hx(client, method, url, data=None):
    kwargs = {"headers": {"HX-Request": "true"}}
    if data is not None:
        kwargs["data"] = data
    return getattr(client, method)(url, **kwargs)


@pytest.mark.django_db
class TestAccess:
    def test_anonymous_sent_to_login(self, client):
        response = client.get(SUPPORT_URL, follow=True)
        assert "Запись на лабораторные" in response.content.decode()

    def test_support_is_students_only(self, client, staff):
        client.force_login(staff)
        response = client.get(SUPPORT_URL, follow=True)
        assert "Доступ только для студентов." in response.content.decode()

    def test_staff_support_denied_for_student(self, client, student):
        client.force_login(student)
        response = client.get(STAFF_URL, follow=True)
        assert "Доступ только для сотрудников" in response.content.decode()


@pytest.mark.django_db
class TestStudentFlow:
    def test_empty_list(self, client, bound_student):
        client.force_login(bound_student)
        page = client.get(SUPPORT_URL).content.decode()
        assert "Обращений пока нет" in page

    def test_create_ticket(self, client, bound_student, room):
        client.force_login(bound_student)
        response = client.post(
            CREATE_URL,
            {"subject": "Не работает стенд", "body": "Стенд 3 не запускается", "training_center": room.training_center.pk},
        )
        ticket = SupportTicket.objects.get(student=bound_student)
        assert response.status_code == 302
        assert response.url == f"/support/{ticket.pk}/"
        assert ticket.status == SupportTicket.Status.OPEN
        assert ticket.training_center == room.training_center

    def test_create_denied_for_foreign_tc(self, client, bound_student, other_tc):
        client.force_login(bound_student)
        response = client.post(
            CREATE_URL,
            {"subject": "Тема", "body": "Текст", "training_center": other_tc.pk},
        )
        assert response.status_code == 200
        assert "Выберите корректный вариант" in response.content.decode()
        assert not SupportTicket.objects.exists()

    def test_create_without_centers_redirects(self, client, db):
        orphan = User.objects.create_user(
            email="orphan@stud.spmi.ru", password="pass", first_name="Б", last_name="Ез"
        )
        client.force_login(orphan)
        response = client.get(CREATE_URL, follow=True)
        assert "не привязаны к лабораториям" in response.content.decode()

    def test_list_shows_ticket(self, client, bound_student, room):
        make_ticket(bound_student, tc=room.training_center, subject="Стенд 3")
        client.force_login(bound_student)
        page = client.get(SUPPORT_URL).content.decode()
        assert "Стенд 3" in page

    def test_chat_page_renders_bubbles(self, client, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center, body="Стенд не запускается")
        client.force_login(bound_student)
        page = client.get(f"/support/{ticket.pk}/").content.decode()
        assert ticket.subject in page
        assert "Стенд не запускается" in page
        assert 'hx-trigger="every 4s"' in page
        assert "bubble--own" in page  # первый пузырь — свой

    def test_chat_denied_for_other_student(self, client, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        stranger = User.objects.create_user(
            email="stranger@stud.spmi.ru", password="pass", first_name="Ч", last_name="Уж"
        )
        client.force_login(stranger)
        assert client.get(f"/support/{ticket.pk}/").status_code == 404

    def test_reply_fragment_via_htmx(self, client, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(bound_student)
        response = _hx(client, "post", f"/support/{ticket.pk}/reply/", {"body": "Дополнение"})
        page = response.content.decode()
        assert response.status_code == 200
        assert "<!DOCTYPE" not in page  # только фрагмент
        assert "Дополнение" in page
        assert SupportMessage.objects.filter(ticket=ticket, body="Дополнение").exists()

    def test_reply_reopens_resolved_ticket(self, client, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        ticket.status = SupportTicket.Status.RESOLVED
        ticket.save(update_fields=["status"])
        client.force_login(bound_student)
        _hx(client, "post", f"/support/{ticket.pk}/reply/", {"body": "А всё ещё не работает"})
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN


@pytest.mark.django_db
class TestStaffMessenger:
    def test_split_view_scoped_by_tc(self, client, staff, bound_student, room, other_tc):
        own = make_ticket(bound_student, tc=room.training_center, subject="Свой тред")
        stranger = User.objects.create_user(
            email="stranger2@stud.spmi.ru", password="pass", first_name="Ч", last_name="Уж"
        )
        make_ticket(stranger, tc=other_tc, subject="Чужой тред")
        client.force_login(staff)
        page = client.get(STAFF_URL).content.decode()
        assert "Свой тред" in page
        assert "Чужой тред" not in page
        assert f'href="{STAFF_URL}?ticket={own.pk}"' in page
        assert 'hx-trigger="every 4s"' in page

    def test_chat_fragment_without_shell(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center, body="Стенд не запускается")
        client.force_login(staff)
        response = _hx(client, "get", f"/staff/support/{ticket.pk}/chat/")
        page = response.content.decode()
        assert "<!DOCTYPE" not in page
        assert "Стенд не запускается" in page
        assert f"/staff/support/{ticket.pk}/reply/" in page
        assert "bubble--own" not in page.split("Стенд не запускается")[0]  # чужой пузырь — не own

    def test_chat_denied_for_other_tc_staff(self, client, other_staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(other_staff)
        page = _hx(client, "get", f"/staff/support/{ticket.pk}/chat/").content.decode()
        assert "недоступно" in page

    def test_opening_chat_marks_read(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        _hx(client, "get", f"/staff/support/{ticket.pk}/chat/")
        ticket.refresh_from_db()
        assert ticket.staff_read_at is not None

    def test_unread_dot_appears_and_clears(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        threads = _hx(client, "get", "/staff/support/fragments/threads/").content.decode()
        assert "dot-unread" in threads
        _hx(client, "get", f"/staff/support/{ticket.pk}/chat/")
        threads = _hx(client, "get", "/staff/support/fragments/threads/").content.decode()
        assert "dot-unread" not in threads

    def test_new_student_message_unread_again(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        _hx(client, "get", f"/staff/support/{ticket.pk}/chat/")  # прочитано
        message = SupportMessage.objects.create(ticket=ticket, author=bound_student, body="Ещё вопрос")
        SupportMessage.objects.filter(pk=message.pk).update(
            created_at=timezone.now() + timedelta(seconds=1)
        )  # строго позже отметки о прочтении — без гонки микросекунд
        threads = _hx(client, "get", "/staff/support/fragments/threads/").content.decode()
        assert "dot-unread" in threads

    def test_overdue_badge_in_threads(self, client, staff, bound_student, room):
        make_ticket(bound_student, tc=room.training_center, created_days_ago=5)
        client.force_login(staff)
        threads = _hx(client, "get", "/staff/support/fragments/threads/").content.decode()
        assert "просрочено" in threads

    def test_active_thread_highlight(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        threads = _hx(
            client, "get", "/staff/support/fragments/threads/?active=" + str(ticket.pk)
        ).content.decode()
        assert "thread-item--current" in threads

    def test_staff_reply_resolves_and_renders(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        response = _hx(client, "post", f"/staff/support/{ticket.pk}/reply/", {"body": "Починим к пятнице"})
        page = response.content.decode()
        assert "<!DOCTYPE" not in page
        assert "Починим к пятнице" in page
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.RESOLVED

    def test_staff_reply_regular_post_redirects(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        response = client.post(f"/staff/support/{ticket.pk}/reply/", {"body": "Ответ"})
        assert response.status_code == 302
        assert response.url == STAFF_URL

    def test_status_change_via_htmx(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        response = _hx(client, "post", f"/staff/support/{ticket.pk}/status/", {"status": "IN_PROGRESS"})
        assert "badge--in_progress" in response.content.decode()
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.IN_PROGRESS

    def test_invalid_status_shows_error(self, client, staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(staff)
        response = _hx(client, "post", f"/staff/support/{ticket.pk}/status/", {"status": "OPEN"})
        assert "Недопустимый статус" in response.content.decode()

    def test_reply_denied_for_other_tc_staff(self, client, other_staff, bound_student, room):
        ticket = make_ticket(bound_student, tc=room.training_center)
        client.force_login(other_staff)
        response = client.post(f"/staff/support/{ticket.pk}/reply/", {"body": "Ответ"})
        assert response.status_code == 302
        assert not SupportMessage.objects.exists()

