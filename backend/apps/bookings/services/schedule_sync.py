"""Reconcile слотов после изменений расписания и параметров ЛР.

Дополнение insert-only генератора (scheduling/services/slot_generation.py):
генератор только создаёт слоты; здесь — отмена осиротевших слотов и
синхронизация параметров существующих. Отменённые (CANCELLED) слоты
никогда не ре-открываются — анти-«зомби» относительно v1.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.academics.models import LabWork
from apps.bookings.models import BookingStatus
from apps.bookings.services.booking import BookingService
from apps.scheduling.models import (
    LabSession,
    LabSessionStatus,
    Room,
    Semester,
)
from apps.scheduling.services.entry_match import matching_entries_for_session
from apps.scheduling.services.slot_generation import generated_session_capacity

LIVE_STATUSES = frozenset({LabSessionStatus.OPEN, LabSessionStatus.DRAFT})


def sync_future_sessions_for_slot(
    *,
    room: Room,
    semester: Semester,
    weekday: int,
    note: str = "Изменение расписания лаборатории",
) -> int:
    """Будущие слоты комнаты на день недели: без покрывающей записи — отмена
    (активные записи → SLOT_CANCELLED, очередь сбрасывается), с записью —
    синхронизация преподавателя/вместимости. Отменённые слоты не трогаем.

    Возвращает число отменённых слотов.
    """
    now = timezone.now()
    service = BookingService()
    sessions = (
        LabSession.objects.filter(room=room, semester=semester, starts_at__gt=now)
        .select_related("lab_work")
        .order_by("starts_at")
    )
    cancelled = 0
    for session in sessions:
        if timezone.localtime(session.starts_at).weekday() != weekday:
            continue
        if session.status not in LIVE_STATUSES:
            continue
        matched = matching_entries_for_session(session)
        if not matched:
            service.cancel_session_bookings(session, note=note)
            cancelled += 1
            continue
        entry = matched[0]
        update_fields: list[str] = []
        if session.teacher_id != entry.teacher_id:
            session.teacher = entry.teacher
            update_fields.append("teacher")
        desired_capacity = generated_session_capacity(entry, session.lab_work)
        if session.capacity != desired_capacity:
            session.capacity = desired_capacity
            update_fields.append("capacity")
        if not session.bookings.filter(current_status=BookingStatus.BOOKED).exists():
            desired_end = session.starts_at + timedelta(minutes=session.lab_work.duration_minutes)
            if session.ends_at != desired_end:
                session.ends_at = desired_end
                update_fields.append("ends_at")
        if update_fields:
            session.save(update_fields=update_fields)
    return cancelled


def sync_open_session_capacities(lab_work: LabWork) -> int:
    """Вместимость будущих открытых слотов ЛР после изменения его лимита мест.

    Формула — как у генератора: min(capacity записи расписания, capacity ЛР)
    по покрывающей записи; слот без записи не трогаем (им занимаются
    reconcile-функции расписания).
    """
    now = timezone.now()
    updated = 0
    sessions = LabSession.objects.filter(
        lab_work=lab_work,
        starts_at__gt=now,
        status=LabSessionStatus.OPEN,
    ).select_related("lab_work")
    for session in sessions:
        matched = matching_entries_for_session(session)
        if not matched:
            continue
        new_capacity = generated_session_capacity(matched[0], session.lab_work)
        if session.capacity != new_capacity:
            session.capacity = new_capacity
            session.save(update_fields=["capacity"])
            updated += 1
    return updated


def sync_open_session_durations(lab_work: LabWork) -> int:
    """Окончание будущих открытых слотов ЛР без активных записей (порт v1)."""
    now = timezone.now()
    sessions = (
        LabSession.objects.filter(
            lab_work=lab_work,
            starts_at__gt=now,
            status=LabSessionStatus.OPEN,
        )
        .annotate(
            active_bookings=Count(
                "bookings", filter=Q(bookings__current_status=BookingStatus.BOOKED)
            )
        )
        .filter(active_bookings=0)
    )
    updated = 0
    for session in sessions:
        new_ends_at = session.starts_at + timedelta(minutes=lab_work.duration_minutes)
        if session.ends_at != new_ends_at:
            session.ends_at = new_ends_at
            session.save(update_fields=["ends_at"])
            updated += 1
    return updated
