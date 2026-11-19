"""Массовые операции со слотами: серии, копирование, массовая отмена.

Все разрушительные операции двухфазные: ``force=False`` возвращает preview,
``force=True`` выполняет отмену.
"""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus, WaitlistEntry
from apps.scheduling.models import (
    Holiday,
    LabSession,
    LabSessionStatus,
    Room,
)
from apps.scheduling.services.session_admin import (
    SessionError,
    cancel_session,
    clean_session_payload,
    create_session,
)


class BulkError(Exception):
    """Ошибка массовой операции со слотами."""


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise BulkError(f"Некорректная дата: {value}.") from exc


def _parse_time(value) -> time:
    if isinstance(value, time):
        return value
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            continue
    raise BulkError(f"Некорректное время: {value}.")


def _date_step(repeat: str) -> int:
    if repeat == "NONE":
        return 0
    if repeat == "WEEKLY":
        return 7
    if repeat == "BIWEEKLY":
        return 14
    raise BulkError(f"Некорректный тип повтора: {repeat}.")


def _occurrence_dates(
    start_date: date,
    repeat: str,
    occurrences: int,
    semester,
) -> list[date]:
    step = _date_step(repeat)
    if repeat == "NONE" and occurrences != 1:
        raise BulkError("Для режима 'NONE' количество слотов должно быть 1.")
    dates: list[date] = []
    cursor = start_date
    for _ in range(occurrences):
        if cursor > semester.end_date or cursor < semester.start_date:
            raise BulkError(
                f"Дата {cursor:%d.%m.%Y} выходит за границы семестра "
                f"({semester.start_date:%d.%m.%Y} – {semester.end_date:%d.%m.%Y})."
            )
        dates.append(cursor)
        cursor += timedelta(days=step)
    return dates


def _create_on_date(laboratory, data: dict, target_date: date, *, actor=None):
    """Создать слот и добавить дату к ошибкам валидации."""
    try:
        return create_session(laboratory, data, actor=actor)
    except SessionError as exc:
        raise BulkError(f"{target_date:%d.%m.%Y}: {exc}") from exc


@transaction.atomic
def create_series(laboratory, data: dict, *, actor=None) -> dict:
    """Создать серию слотов.

    Параметры ``data``:
    - lab_work_id, room_id, semester_id
    - teacher_id (опц.), allowed_student_groups (опц.)
    - start_date, start_time
    - repeat: NONE | WEEKLY | BIWEEKLY
    - occurrences (≤ 30)
    - skip_holidays (bool, default True)
    """
    repeat = data.get("repeat", "NONE")
    try:
        occurrences = int(data.get("occurrences", 1))
    except (ValueError, TypeError) as exc:
        raise BulkError("Количество слотов должно быть числом.") from exc
    if occurrences < 1 or occurrences > 30:
        raise BulkError("Количество слотов: от 1 до 30.")

    start_date = _parse_date(data.get("start_date"))
    start_time = _parse_time(data.get("start_time"))
    skip_holidays = str(data.get("skip_holidays", "true")).lower() in ("true", "1", "on", "yes")

    # Проверим первый слот, чтобы валидировать базовые поля
    first_payload = clean_session_payload(
        laboratory,
        {
            **data,
            "date": start_date.isoformat(),
            "time": data.get("start_time"),
        },
    )
    semester = first_payload["semester"]
    lab_work = first_payload["lab_work"]
    room = first_payload["room"]
    base_data = {
        "lab_work_id": lab_work.pk,
        "room_id": room.pk,
        "semester_id": semester.pk,
        "teacher_id": first_payload["teacher"].pk if first_payload["teacher"] else None,
        "booking_mode": first_payload["booking_mode"],
        "group_part": first_payload["group_part"],
        "occupant_group_id": (
            first_payload["occupant_group"].pk
            if first_payload["occupant_group"]
            else None
        ),
    }
    if first_payload["allowed_groups"]:
        base_data["allowed_student_groups"] = [g.pk for g in first_payload["allowed_groups"]]

    dates = _occurrence_dates(start_date, repeat, occurrences, semester)
    holiday_dates = set(Holiday.objects.values_list("date", flat=True)) if skip_holidays else set()

    created = 0
    overflows = 0
    skipped_holidays = 0
    skipped_duplicates = 0
    tz = timezone.get_current_timezone()

    for occurrence_date in dates:
        if skip_holidays and occurrence_date in holiday_dates:
            skipped_holidays += 1
            continue
        starts_at = timezone.make_aware(datetime.combine(occurrence_date, start_time), tz)
        if LabSession.objects.filter(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts_at,
        ).exclude(status=LabSessionStatus.CANCELLED).exists():
            skipped_duplicates += 1
            continue
        occurrence_data = {
            **base_data,
            "starts_at": starts_at.isoformat(),
        }
        session = _create_on_date(laboratory, occurrence_data, occurrence_date, actor=actor)
        if session.capacity > session.room.capacity:
            overflows += 1
        created += 1

    return {
        "created": created,
        "overflows": overflows,
        "skipped_holidays": skipped_holidays,
        "skipped_duplicates": skipped_duplicates,
    }


def _copy_session(session: LabSession, target_date: date) -> dict:
    local_start = timezone.localtime(session.starts_at)
    return {
        "lab_work_id": session.lab_work_id,
        "room_id": session.room_id,
        "semester_id": session.semester_id,
        "teacher_id": session.teacher_id,
        "date": target_date.isoformat(),
        "time": local_start.strftime("%H:%M"),
        "booking_mode": session.booking_mode,
        "group_part": session.group_part,
        "allowed_student_groups": list(
            session.allowed_student_groups.values_list("pk", flat=True)
        ),
        "occupant_group_id": session.occupant_group_id,
    }


@transaction.atomic
def _copy_sessions(
    laboratory,
    sessions: Iterable[LabSession],
    date_shift: timedelta,
    *,
    actor=None,
) -> dict:
    created = 0
    skipped_duplicates = 0
    skipped_holidays = 0
    holiday_dates = set(Holiday.objects.values_list("date", flat=True))
    for session in sessions:
        source_date = timezone.localtime(session.starts_at).date()
        target_date = source_date + date_shift
        if target_date in holiday_dates:
            skipped_holidays += 1
            continue
        data = _copy_session(session, target_date)
        starts_at = timezone.make_aware(
            datetime.combine(target_date, _parse_time(data["time"])),
            timezone.get_current_timezone(),
        )
        if LabSession.objects.filter(
            lab_work_id=data["lab_work_id"],
            room_id=data["room_id"],
            semester_id=data["semester_id"],
            starts_at=starts_at,
        ).exclude(status=LabSessionStatus.CANCELLED).exists():
            skipped_duplicates += 1
            continue
        _create_on_date(laboratory, data, target_date, actor=actor)
        created += 1
    return {
        "created": created,
        "skipped_holidays": skipped_holidays,
        "skipped_duplicates": skipped_duplicates,
    }


def copy_day(
    laboratory,
    semester,
    room: Room,
    source_date: date,
    target_date: date,
    *,
    actor=None,
) -> dict:
    """Копировать OPEN/DRAFT-слоты комнаты за один день на другой день семестра."""
    if room.laboratory_id and room.laboratory_id != laboratory.pk:
        raise BulkError("Аудитория принадлежит другой лаборатории.")
    if target_date < semester.start_date or target_date > semester.end_date:
        raise BulkError("Целевая дата вне границ семестра.")
    sessions = LabSession.objects.filter(
        room=room,
        semester=semester,
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
        starts_at__date=source_date,
    )
    shift = target_date - source_date
    return _copy_sessions(laboratory, sessions, shift, actor=actor)


def copy_week(
    laboratory,
    semester,
    room: Room,
    source_week_start: date,
    target_week_start: date,
    *,
    actor=None,
) -> dict:
    """Копировать неделю слотов комнаты на другую неделю в том же семестре.

    Обе даты должны быть понедельниками: иначе непонятно, какую неделю
    копировать пользователь, указавший середину недели.
    """
    if room.laboratory_id and room.laboratory_id != laboratory.pk:
        raise BulkError("Аудитория принадлежит другой лаборатории.")
    if source_week_start.weekday() != 0 or target_week_start.weekday() != 0:
        raise BulkError("Даты недели должны быть понедельниками.")
    if target_week_start < semester.start_date or target_week_start > semester.end_date:
        raise BulkError("Целевая неделя вне границ семестра.")
    source_dates = {source_week_start + timedelta(days=i) for i in range(7)}
    sessions = LabSession.objects.filter(
        room=room,
        semester=semester,
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
        starts_at__date__in=source_dates,
    )
    shift = target_week_start - source_week_start
    return _copy_sessions(laboratory, sessions, shift, actor=actor)


def _cancel_scope(
    laboratory,
    semester,
    *,
    lab_work=None,
    room=None,
    from_date=None,
    to_date=None,
):
    if lab_work is None and room is None:
        raise BulkError("Укажите лабораторную работу или аудиторию для отмены серии.")
    if from_date is None or to_date is None:
        raise BulkError("Укажите обе границы периода.")
    from_date = _parse_date(from_date)
    to_date = _parse_date(to_date)
    if from_date > to_date:
        raise BulkError("Начало периода не может быть позже конца.")

    qs = LabSession.objects.filter(
        semester=semester,
        starts_at__date__gte=from_date,
        starts_at__date__lte=to_date,
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
    )
    if lab_work is not None:
        qs = qs.filter(lab_work=lab_work)
    if room is not None:
        qs = qs.filter(room=room)
    # скоуп лаборатории: либо через аудиторию, либо через ЛР
    qs = qs.filter(
        django_models.Q(room__laboratory=laboratory) | django_models.Q(lab_work__laboratories=laboratory)
    ).distinct()
    return qs, from_date, to_date


@transaction.atomic
def cancel_series(
    laboratory,
    semester,
    *,
    lab_work=None,
    room=None,
    from_date=None,
    to_date=None,
    actor=None,
    force=False,
) -> dict:
    """Двухфазная отмена серии слотов.

    ``force=False`` — preview; ``force=True`` — выполнить отмену.
    Preview считает записи студентов, а не слоты: подпись «записей» должна
    совпадать с тем, что увидит завлаб.
    """
    if room is not None and room.laboratory_id and room.laboratory_id != laboratory.pk:
        raise BulkError("Аудитория принадлежит другой лаборатории.")
    qs, _from_date, _to_date = _cancel_scope(
        laboratory, semester, lab_work=lab_work, room=room, from_date=from_date, to_date=to_date
    )
    sessions = list(qs)
    booked = Booking.objects.filter(
        lab_session__in=sessions, current_status=BookingStatus.BOOKED
    ).count()
    waitlist_count = WaitlistEntry.objects.filter(lab_session__in=sessions).count()
    preview = {
        "sessions": len(sessions),
        "booked_bookings": booked,
        "waitlist_entries": waitlist_count,
    }
    if not force:
        return preview

    cancelled = 0
    for session in sessions:
        cancel_session(laboratory, session, actor=actor, note="Массовая отмена серии")
        cancelled += 1
    return {**preview, "cancelled": cancelled}


def cancel_day(
    laboratory,
    semester,
    room: Room,
    day: date,
    *,
    actor=None,
    force=False,
) -> dict:
    """Двухфазная отмена всех слотов комнаты за день."""
    return cancel_series(
        laboratory,
        semester,
        room=room,
        from_date=day,
        to_date=day,
        actor=actor,
        force=force,
    )
