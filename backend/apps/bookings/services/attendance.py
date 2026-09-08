"""Посещаемость: счётчики неявок по лаборатории, объяснительные, авто-«Посетил»."""

import logging

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.academics.scope import resolve_staff_laboratory
from apps.bookings.models import (
    NO_SHOW_EXPLANATION_THRESHOLD,
    AuditLog,
    Booking,
    BookingStatus,
    StudentLabAttendance,
)
from apps.bookings.services.errors import BookingError
from apps.bookings.services.session_availability import is_ready_for_auto_visited
from apps.scheduling.models import (
    AutoVisitedMode,
    Laboratory,
    LaboratoryBookingSettings,
    LabSession,
    LabSessionStatus,
)
from apps.users.models import UserRole
from apps.users.roles import staff_can_modify_bookings

logger = logging.getLogger(__name__)


# --- Счётчики неявок (вызываются из транзакции BookingService) ----------------


def resolve_booking_laboratory(booking: Booking) -> Laboratory | None:
    """Лаборатория для учёта посещаемости: аудитория, иначе ЛР/дисциплина."""
    if booking.room.laboratory_id:
        # По id: select_related(room__laboratory) под FOR UPDATE нельзя
        # (nullable FK → LEFT JOIN → ошибка PostgreSQL).
        return Laboratory.objects.filter(pk=booking.room.laboratory_id).first()
    if booking.lab_work_id:
        lab = booking.lab_work.laboratories.order_by("name").first()
        if lab:
            return lab
    if booking.discipline_id:
        return booking.discipline.laboratories.order_by("name").first()
    return None


def get_or_create_lab_attendance(student, laboratory: Laboratory) -> StudentLabAttendance:
    attendance = (
        StudentLabAttendance.objects.select_for_update()
        .filter(student=student, laboratory=laboratory)
        .first()
    )
    if attendance is not None:
        return attendance
    attendance, _ = StudentLabAttendance.objects.get_or_create(
        student=student, laboratory=laboratory
    )
    return StudentLabAttendance.objects.select_for_update().get(pk=attendance.pk)


def adjust_no_show_count(student, laboratory: Laboratory | None, delta: int) -> bool:
    """Счётчик неявок по лаборатории + флаг объяснительной.

    Returns True, если флаг «объяснительная» только что стал обязательным.
    """
    if delta == 0 or laboratory is None:
        return False
    attendance = get_or_create_lab_attendance(student, laboratory)
    new_count = max(0, attendance.no_show_count + delta)
    update_fields = ["no_show_count", "updated_at"]
    attendance.no_show_count = new_count
    explanation_just_required = False
    if new_count >= NO_SHOW_EXPLANATION_THRESHOLD and not attendance.explanation_required:
        attendance.explanation_required = True
        update_fields.append("explanation_required")
        explanation_just_required = True
    elif new_count < NO_SHOW_EXPLANATION_THRESHOLD and attendance.explanation_required:
        attendance.explanation_required = False
        update_fields.append("explanation_required")
    attendance.save(update_fields=update_fields)
    return explanation_just_required


def has_had_no_show_for_lab_work(student, lab_work_id: int) -> bool:
    return Booking.objects.filter(
        student=student, lab_work_id=lab_work_id, had_no_show=True
    ).exists()


# --- Объяснительные и авто-«Посетил» --------------------------------------------


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
    from apps.bookings.services.booking import BookingService  # поздний импорт: без циклов

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
