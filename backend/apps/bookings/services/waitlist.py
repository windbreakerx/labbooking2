"""Очередь на слот: вступление, промоушен после коммита, выкидывание.

Промоушен выполняется отдельной транзакцией ПОСЛЕ коммита отмены
(transaction.on_commit): ретрай deadlock реально работает, отмена
не откатывается из-за сбоя промоушена. При отмене слота целиком очередь
выкидывается с уведомлением — промоутить некуда (слот CANCELLED).
"""

from django.db import transaction

from apps.academics.scope import student_can_access_lab_work
from apps.bookings.models import AuditLog, BookingStatus, WaitlistEntry
from apps.bookings.notifications import notify_waitlist_event
from apps.bookings.services.errors import BookingError
from apps.bookings.services.retry import retry_on_deadlock
from apps.scheduling.models import LabSession
from apps.users.models import UserRole

_NOTIFY_RELATED = ("student", "lab_session", "lab_session__lab_work", "lab_session__room")


def join_waitlist(student, session_id: int) -> WaitlistEntry:
    """Встать в очередь на полный слот."""
    return _join_waitlist_with_retry(student, session_id)


@retry_on_deadlock
def _join_waitlist_with_retry(student, session_id):
    with transaction.atomic():
        return _join_waitlist_impl(student, session_id)


def _join_waitlist_impl(student, session_id) -> WaitlistEntry:
    session = LabSession.objects.select_for_update(of=("self",)).get(pk=session_id)
    if student.role == UserRole.STUDENT and not student_can_access_lab_work(
        student, session.lab_work_id
    ):
        raise BookingError("Лабораторная работа недоступна для вашей группы.")
    if session.bookings.filter(current_status=BookingStatus.BOOKED).count() < session.capacity:
        raise BookingError("В слоте есть свободные места — запишитесь напрямую.")
    if WaitlistEntry.objects.filter(lab_session=session, student=student).exists():
        raise BookingError("Вы уже в очереди на этот слот.")
    position = WaitlistEntry.objects.filter(lab_session=session).count() + 1
    entry = WaitlistEntry.objects.create(lab_session=session, student=student, position=position)
    AuditLog.objects.create(
        actor=student,
        action="waitlist.join",
        entity_type="WaitlistEntry",
        entity_id=entry.pk,
        payload={},
    )
    return entry


def schedule_waitlist_promotion(session_id: int) -> None:
    """Промоушен первого в очереди после коммита текущей транзакции.

    Вне atomic-блока on_commit выполняет колбэк немедленно.
    """
    transaction.on_commit(lambda: promote_waitlist(session_id))


def promote_waitlist(session_id: int) -> None:
    """Записать первого в очереди на освободившееся место.

    Если записаться не удалось (окно записи, лимиты, пересечения) —
    участник выбывает из очереди с уведомлением.
    """
    _promote_waitlist_with_retry(session_id)


@retry_on_deadlock
def _promote_waitlist_with_retry(session_id: int) -> None:
    from apps.bookings.services.booking import BookingService

    entry = (
        WaitlistEntry.objects.select_related(*_NOTIFY_RELATED)
        .filter(lab_session_id=session_id)
        .order_by("position")
        .first()
    )
    if entry is None:
        return
    try:
        BookingService().create_booking(entry.student, session_id)
    except BookingError:
        notify_waitlist_event(entry, "dropped")
        entry.delete()
    else:
        entry.delete()  # «Вы записаны» уже отправлено из create_booking


def drop_session_waitlist(session) -> int:
    """Выкинуть всю очередь слота (слот отменяется) с уведомлением."""
    dropped = 0
    for entry in session.waitlist.select_related(*_NOTIFY_RELATED).order_by("position"):
        notify_waitlist_event(entry, "session_cancelled")
        entry.delete()
        dropped += 1
    return dropped
