"""Посещаемость: объяснительные и авто-«Посетил» — операции поверх BookingService."""

import logging

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.academics.scope import resolve_staff_laboratory
from apps.bookings.models import (
    AuditLog,
    Booking,
    BookingStatus,
    Laboratory,
    StudentLabAttendance,
)
from apps.bookings.services.booking import BookingError, BookingService
from apps.bookings.services.session_availability import is_ready_for_auto_visited
from apps.scheduling.models import (
    AutoVisitedMode,
    LaboratoryBookingSettings,
    LabSession,
    LabSessionStatus,
)
from apps.users.models import UserRole
from apps.users.roles import staff_can_modify_bookings

logger = logging.getLogger(__name__)


@transaction.atomic
def clear_explanation_required(
    actor,
    student,
    note: str = "",
    laboratory: Laboratory | None = None,
):
    """Объяснительная получена: снять флаг, счётчик неявок сохранить для аналитики."""
    if actor and not staff_can_modify_bookings(actor):
        raise BookingError("Недостаточно прав для снятия отметки об объяснительной.")
    if student.role != UserRole.STUDENT:
        raise BookingError("Отметку об объяснительной можно снять только у студента.")
    lab = laboratory or (resolve_staff_laboratory(actor) if actor else None)
    if lab is None:
        raise BookingError(
            "Не удалось определить лабораторию. Привяжите лабораторию к профилю сотрудника."
        )
    attendance = (
        StudentLabAttendance.objects.select_for_update()
        .filter(student=student, laboratory=lab)
        .first()
    )
    if attendance is None or not attendance.explanation_required:
        return student

    attendance.explanation_required = False
    attendance.save(update_fields=["explanation_required", "updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="student.explanation_cleared",
        entity_type="StudentLabAttendance",
        entity_id=attendance.pk,
        payload={
            "note": note or "Объяснительная получена",
            "laboratory_id": lab.pk,
            "no_show_count_preserved": attendance.no_show_count,
        },
    )
    return student


def session_laboratory_id(session: LabSession) -> int | None:
    """Лаборатория сессии: аудитория, иначе первая лаборатория ЛР."""
    if session.room.laboratory_id:
        return session.room.laboratory_id
    if not session.lab_work_id:
        return None
    return session.lab_work.laboratories.order_by("name").values_list("id", flat=True).first()


def mark_visited_for_ended_sessions() -> int:
    """Авто-«Посетил» по настройкам лаборатории.

    В MANUAL завершённые OPEN-сессии закрываются без смены статуса записей;
    после включения авто-режима догоняются и CLOSED-сессии с записями BOOKED.
    """
    now = timezone.now()
    service = BookingService()
    count = 0
    booked_exists = Booking.objects.filter(
        lab_session_id=OuterRef("pk"), current_status=BookingStatus.BOOKED
    )
    sessions = (
        LabSession.objects.filter(ends_at__lte=now)
        .annotate(_has_booked=Exists(booked_exists))
        .filter(Q(status=LabSessionStatus.OPEN) | Q(status=LabSessionStatus.CLOSED, _has_booked=True))
        .select_related("room", "lab_work")
        .prefetch_related("lab_work__laboratories")
    )
    settings_by_lab: dict[int, str] = {
        row["laboratory_id"]: row["auto_visited_mode"]
        for row in LaboratoryBookingSettings.objects.values("laboratory_id", "auto_visited_mode")
    }
    for session in sessions:
        mode = settings_by_lab.get(session_laboratory_id(session) or 0, AutoVisitedMode.MANUAL)
        if mode == AutoVisitedMode.MANUAL:
            if session.status == LabSessionStatus.OPEN:
                session.status = LabSessionStatus.CLOSED
                session.save(update_fields=["status"])
            continue
        if not is_ready_for_auto_visited(session, now, auto_visited_mode=mode):
            continue
        for booking in session.bookings.filter(current_status=BookingStatus.BOOKED):
            try:
                service.change_status(booking, BookingStatus.VISITED, note="Автоматически")
                count += 1
            except BookingError:
                logger.exception("Auto-visited failed for booking %s", booking.pk)
        if session.status != LabSessionStatus.CLOSED:
            session.status = LabSessionStatus.CLOSED
            session.save(update_fields=["status"])
    return count
