"""Обращения: создание, ответы, статусы, overdue за 24 рабочих часа (без выходных).

Мессенджер (День 6) строится поверх этих операций: ответ сотрудника → RESOLVED,
сообщение студента → авто-реопен OPEN.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.academics.scope import student_support_training_centers_qs
from apps.bookings.models import SupportMessage, SupportTicket
from apps.bookings.scope import staff_support_tickets_qs
from apps.users.roles import is_staff_user


class SupportError(Exception):
    pass


def _skip_to_next_weekday(dt: datetime) -> datetime:
    while dt.weekday() >= 5:
        dt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt


def add_business_hours(start: datetime, hours: int) -> datetime:
    """Прибавить часы, считая только будни; выходные пропускаются целиком."""
    current = timezone.localtime(start)
    remaining = timedelta(hours=hours)
    while remaining > timedelta(0):
        current = _skip_to_next_weekday(current)
        day_end = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        chunk = min(remaining, day_end - current)
        current += chunk
        remaining -= chunk
    return current


def is_support_ticket_overdue(ticket, *, now: datetime | None = None) -> bool:
    """Открытое обращение без ответа дольше 24 рабочих часов."""
    if ticket.status != SupportTicket.Status.OPEN:
        return False
    deadline = add_business_hours(ticket.created_at, 24)
    return timezone.localtime(now or timezone.now()) > deadline


def _require_ticket_in_scope(ticket, staff) -> None:
    if not staff_support_tickets_qs(staff).filter(pk=ticket.pk).exists():
        raise SupportError("Обращение недоступно в вашей зоне доступа.")


@transaction.atomic
def create_ticket(student, *, subject: str, body: str, training_center) -> SupportTicket:
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise SupportError("Заполните тему и текст обращения.")
    if training_center is None:
        raise SupportError("Выберите лабораторию.")
    if not student_support_training_centers_qs(student).filter(pk=training_center.pk).exists():
        raise SupportError("Выбранная лаборатория недоступна для вашей группы.")
    return SupportTicket.objects.create(
        student=student,
        subject=subject,
        body=body,
        training_center=training_center,
    )


@transaction.atomic
def student_reply(ticket, student, body: str) -> SupportMessage:
    if ticket.student_id != student.pk:
        raise SupportError("Это не ваше обращение.")
    body = (body or "").strip()
    if not body:
        raise SupportError("Сообщение не может быть пустым.")
    message = SupportMessage.objects.create(ticket=ticket, author=student, body=body)
    # Авто-реопен: новое сообщение студента возвращает тикет в OPEN.
    if ticket.status != SupportTicket.Status.OPEN:
        ticket.status = SupportTicket.Status.OPEN
        ticket.save(update_fields=["status", "updated_at"])
    return message


@transaction.atomic
def staff_reply(ticket, staff, body: str) -> SupportMessage:
    if not is_staff_user(staff):
        raise SupportError("Ответ доступен только сотрудникам.")
    _require_ticket_in_scope(ticket, staff)
    body = (body or "").strip()
    if not body:
        raise SupportError("Сообщение не может быть пустым.")
    message = SupportMessage.objects.create(ticket=ticket, author=staff, body=body)
    ticket.status = SupportTicket.Status.RESOLVED
    ticket.save(update_fields=["status", "updated_at"])
    return message


@transaction.atomic
def staff_set_status(ticket, staff, status: str) -> SupportTicket:
    """Ручные переходы сотрудника: IN_PROGRESS / RESOLVED / CLOSED."""
    if not is_staff_user(staff):
        raise SupportError("Изменение статуса доступно только сотрудникам.")
    _require_ticket_in_scope(ticket, staff)
    allowed = {
        SupportTicket.Status.IN_PROGRESS,
        SupportTicket.Status.RESOLVED,
        SupportTicket.Status.CLOSED,
    }
    if status not in allowed:
        raise SupportError("Недопустимый статус обращения.")
    ticket.status = status
    ticket.save(update_fields=["status", "updated_at"])
    return ticket


def mark_staff_read(ticket) -> None:
    """Отметить обращение прочитанным сотрудником (снимает точку unread).

    ``update()`` не трогает ``updated_at`` (auto_now) — прочтение не ответ.
    """
    SupportTicket.objects.filter(pk=ticket.pk).update(staff_read_at=timezone.now())
