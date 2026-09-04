"""SupportService: бизнес-часы, overdue, ответы и авто-реопен (порт из v1)."""

from datetime import datetime

import pytest
from django.utils import timezone

from apps.bookings.models import SupportTicket
from apps.bookings.services.support import (
    SupportError,
    add_business_hours,
    create_ticket,
    is_support_ticket_overdue,
    staff_reply,
    staff_set_status,
    student_reply,
)
from apps.users.models import UserRole


def _local(year, month, day, hour=0, minute=0):
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime(year, month, day, hour, minute), tz)


def test_add_business_hours_skips_weekend():
    friday = _local(2026, 6, 26, 10, 0)
    assert add_business_hours(friday, 24) == _local(2026, 6, 29, 10, 0)


def test_add_business_hours_within_single_day():
    monday = _local(2026, 6, 22, 9, 0)
    assert add_business_hours(monday, 6) == _local(2026, 6, 22, 15, 0)


def _make_ticket(student, room, **kwargs):
    return SupportTicket.objects.create(
        student=student,
        subject="Вопрос",
        body="Текст",
        training_center=room.training_center,
        **kwargs,
    )


@pytest.mark.django_db
class TestOverdue:
    def test_open_ticket(self, student, room):
        ticket = _make_ticket(student, room)
        SupportTicket.objects.filter(pk=ticket.pk).update(created_at=_local(2026, 6, 22, 10, 0))
        ticket.refresh_from_db()
        assert not is_support_ticket_overdue(ticket, now=_local(2026, 6, 22, 20, 0))
        assert is_support_ticket_overdue(ticket, now=_local(2026, 6, 23, 11, 0))

    def test_skips_weekend(self, student, room):
        ticket = _make_ticket(student, room)
        SupportTicket.objects.filter(pk=ticket.pk).update(created_at=_local(2026, 6, 26, 10, 0))
        ticket.refresh_from_db()
        assert not is_support_ticket_overdue(ticket, now=_local(2026, 6, 27, 12, 0))
        assert not is_support_ticket_overdue(ticket, now=_local(2026, 6, 29, 9, 0))
        assert is_support_ticket_overdue(ticket, now=_local(2026, 6, 29, 11, 0))

    def test_not_for_resolved(self, student, room):
        ticket = _make_ticket(student, room, status=SupportTicket.Status.RESOLVED)
        SupportTicket.objects.filter(pk=ticket.pk).update(created_at=_local(2026, 6, 1, 10, 0))
        ticket.refresh_from_db()
        assert not is_support_ticket_overdue(ticket, now=_local(2026, 6, 10, 10, 0))


@pytest.mark.django_db
class TestSupportService:
    def test_create_ticket_scopes_training_center(self, student, room, discipline):
        from apps.scheduling.models import LabDisciplineBinding

        LabDisciplineBinding.objects.create(laboratory=room.laboratory, discipline=discipline)
        ticket = create_ticket(
            student, subject="Тема", body="Текст", training_center=room.training_center
        )
        assert ticket.status == SupportTicket.Status.OPEN

        from apps.scheduling.models import TrainingCenter

        foreign_tc = TrainingCenter.objects.create(number=42)
        with pytest.raises(SupportError, match="недоступна"):
            create_ticket(student, subject="Тема", body="Текст", training_center=foreign_tc)

    def test_create_ticket_requires_fields(self, student, room):
        with pytest.raises(SupportError, match="тему"):
            create_ticket(student, subject=" ", body="Текст", training_center=room.training_center)

    def test_student_reply_reopens(self, student, room):
        ticket = _make_ticket(student, room, status=SupportTicket.Status.RESOLVED)
        message = student_reply(ticket, student, "Нужно уточнение")
        assert message.author == student
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.OPEN
        assert ticket.messages.count() == 1

    def test_student_reply_rejects_foreign_ticket(self, student, room, student_group):
        from apps.users.models import User, UserProfile

        other = User.objects.create_user(
            email="other-s@stud.spmi.ru", password="p", first_name="O", last_name="S",
            role=UserRole.STUDENT,
        )
        UserProfile.objects.create(user=other, student_group=student_group)
        ticket = _make_ticket(student, room)
        with pytest.raises(SupportError, match="не ваше"):
            student_reply(ticket, other, "Ответ")

    def test_staff_reply_resolves(self, student, room, staff):
        ticket = _make_ticket(student, room)
        staff_reply(ticket, staff, "Мы разберёмся")
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.RESOLVED
        assert ticket.messages.count() == 1

    def test_staff_reply_rejects_foreign_ticket(self, student, room, staff):
        from apps.scheduling.models import TrainingCenter

        foreign_tc = TrainingCenter.objects.create(number=43)
        ticket = _make_ticket(student, room)
        ticket.training_center = foreign_tc
        ticket.save(update_fields=["training_center"])
        with pytest.raises(SupportError, match="зоне доступа"):
            staff_reply(ticket, staff, "Ответ")

    def test_staff_set_status(self, student, room, staff):
        ticket = _make_ticket(student, room)
        staff_set_status(ticket, staff, SupportTicket.Status.IN_PROGRESS)
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.IN_PROGRESS
        staff_set_status(ticket, staff, SupportTicket.Status.CLOSED)
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        with pytest.raises(SupportError, match="статус"):
            staff_set_status(ticket, staff, SupportTicket.Status.OPEN)
