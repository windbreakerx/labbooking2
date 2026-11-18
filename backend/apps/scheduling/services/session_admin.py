"""Администрирование одного слота: CRUD, валидация и правила правки.

Публичные функции:
- create_session(laboratory, data, actor=None)
- update_session(laboratory, session, data, actor=None)
- cancel_session(laboratory, session, actor=None, note="")
- update_future_session_durations(lab_work)
"""

from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.academics.models import LabWork, LabWorkBookingRelease, Semester, StudentGroup
from apps.bookings.models import (
    AuditLog,
    Booking,
    BookingStatus,
    BookingStatusHistory,
    RegistrationType,
)
from apps.scheduling.models import (
    LabSession,
    LabSessionBookingMode,
    LabSessionStatus,
    Room,
)
from apps.scheduling.services.slot_grid import (
    UNIVERSITY_PAIR_SLOTS,
    pair_start_times_for_duration,
    time_to_minutes,
)
from apps.users.models import User, UserRole


class SessionError(Exception):
    """Ошибка валидации/состояния слота (показывается пользователю)."""


def _parse_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    return None


def _parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_time(value) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def _resolve_starts_at(data: dict) -> datetime:
    starts_at = _parse_datetime(data.get("starts_at"))
    if starts_at is None:
        d = _parse_date(data.get("date"))
        t = _parse_time(data.get("time"))
        if d is None or t is None:
            raise SessionError("Укажите дату и время начала слота.")
        tz = timezone.get_current_timezone()
        starts_at = timezone.make_aware(datetime.combine(d, t), tz)
    return starts_at


def _resolve_semester(data: dict) -> Semester:
    semester_id = data.get("semester") or data.get("semester_id")
    if semester_id is None:
        raise SessionError("Укажите семестр.")
    try:
        return Semester.objects.get(pk=int(semester_id))
    except (ValueError, Semester.DoesNotExist):
        raise SessionError("Семестр не найден.")


def _resolve_lab_work(data: dict, laboratory) -> LabWork:
    lab_work_id = data.get("lab_work") or data.get("lab_work_id")
    if lab_work_id is None:
        raise SessionError("Укажите лабораторную работу.")
    try:
        lab_work = LabWork.objects.prefetch_related("disciplines").get(pk=int(lab_work_id))
    except (ValueError, LabWork.DoesNotExist):
        raise SessionError("Лабораторная работа не найдена.")
    if not lab_work.is_published:
        raise SessionError("Лабораторная работа не опубликована.")
    if lab_work.is_archived:
        raise SessionError("Лабораторная работа в архиве.")
    if not lab_work.laboratories.filter(pk=laboratory.pk).exists():
        raise SessionError("Лабораторная работа не привязана к вашей лаборатории.")
    return lab_work


def _resolve_room(data: dict, laboratory) -> Room:
    room_id = data.get("room") or data.get("room_id")
    if room_id is None:
        raise SessionError("Укажите аудиторию.")
    try:
        room = Room.objects.get(pk=int(room_id))
    except (ValueError, Room.DoesNotExist):
        raise SessionError("Аудитория не найдена.")
    if room.is_blocked:
        raise SessionError("Аудитория заблокирована.")
    if room.training_center_id != laboratory.training_center_id:
        raise SessionError("Аудитория принадлежит другому учебному центру.")
    if room.laboratory_id and room.laboratory_id != laboratory.pk:
        raise SessionError("Аудитория закреплена за другой лабораторией.")
    return room


def _resolve_teacher(data: dict) -> User | None:
    teacher_id = data.get("teacher") or data.get("teacher_id")
    if teacher_id in (None, "", 0):
        return None
    try:
        user = User.objects.get(pk=int(teacher_id))
    except (ValueError, User.DoesNotExist):
        raise SessionError("Преподаватель не найден.")
    if user.role not in (UserRole.TEACHER, UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.SYS_ADMIN):
        raise SessionError("Выбранный пользователь не может быть преподавателем.")
    return user


def _resolve_occupant_group(data: dict) -> StudentGroup | None:
    group_id = data.get("occupant_group") or data.get("occupant_group_id")
    if group_id in (None, "", 0):
        return None
    try:
        return StudentGroup.objects.get(pk=int(group_id))
    except (ValueError, StudentGroup.DoesNotExist):
        raise SessionError("Группа-оккупант не найдена.")


def _resolve_allowed_groups(data: dict) -> list[StudentGroup]:
    raw = data.get("allowed_student_groups") or data.get("allowed_groups") or []
    if isinstance(raw, str):
        raw = [item for item in raw.split(",") if item.strip()]
    ids = []
    for item in raw:
        try:
            ids.append(int(item))
        except (ValueError, TypeError):
            raise SessionError(f"Некорректный идентификатор группы: {item}.")
    if not ids:
        return []
    groups = list(StudentGroup.objects.filter(pk__in=ids))
    if len(groups) != len(ids):
        raise SessionError("Некоторые группы не найдены.")
    return groups


def _validate_stands(lab_work: LabWork, room: Room) -> None:
    """Если у ЛР есть стенды, в аудитории должен стоять хотя бы один из них."""
    if not lab_work.stands.exists():
        return
    if not lab_work.stands.filter(room=room).exists():
        raise SessionError(
            f"В аудитории №{room.number} нет стенда этой лабораторной работы."
        )


def _validate_schedule_conflicts(
    *,
    lab_work: LabWork,
    room: Room,
    teacher: User | None,
    starts_at: datetime,
    ends_at: datetime,
    session: LabSession | None = None,
) -> None:
    """Пересечения: преподаватель, стенды ЛР в этой комнате."""
    overlapping = LabSession.objects.filter(
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    ).exclude(status=LabSessionStatus.CANCELLED)
    if session is not None:
        overlapping = overlapping.exclude(pk=session.pk)

    if teacher is not None and overlapping.filter(teacher=teacher).exists():
        raise SessionError("У преподавателя уже есть пара в это время.")

    stand_ids = set(lab_work.stands.values_list("id", flat=True))
    if not stand_ids:
        return
    _validate_stands(lab_work, room)
    if (
        overlapping.exclude(lab_work_id=lab_work.pk)
        .filter(lab_work__stands__id__in=stand_ids)
        .exists()
    ):
        raise SessionError("Стенд этой лабораторной работы занят другой работой в это время.")
    parallel = overlapping.filter(lab_work_id=lab_work.pk, room=room).count()
    stands_in_room = lab_work.stands.filter(room=room).count()
    if parallel >= stands_in_room:
        raise SessionError(
            "Все стенды этой лабораторной работы в аудитории заняты в это время."
        )


def _is_pair_time(moment: datetime) -> bool:
    local = timezone.localtime(moment)
    starts_at = local.time().replace(second=0, microsecond=0)
    minute_value = time_to_minutes(starts_at)
    for _, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        pair_start_min = time_to_minutes(pair_start)
        pair_end_min = time_to_minutes(pair_end)
        if pair_start_min <= minute_value < pair_end_min:
            offset = minute_value - pair_start_min
            return offset >= 0 and offset % 15 == 0
    return False


def _validate_slot_time(starts_at: datetime, lab_work: LabWork) -> None:
    if not _is_pair_time(starts_at):
        raise SessionError("Начало слота должно попадать на 15-минутную сетку внутри пары.")
    allowed = pair_start_times_for_duration(lab_work.duration_minutes)
    local_time = timezone.localtime(starts_at).time().replace(second=0, microsecond=0)
    if local_time not in allowed:
        raise SessionError(
            f"Лабораторная работа длительностью {lab_work.duration_minutes} мин "
            "не помещается в выбранную пару."
        )


def _validate_semester_bounds(starts_at: datetime, ends_at: datetime, semester: Semester) -> None:
    if timezone.localdate(starts_at) < semester.start_date or timezone.localdate(starts_at) > semester.end_date:
        raise SessionError("Дата слота выходит за границы семестра.")
    if timezone.localdate(ends_at) > semester.end_date:
        raise SessionError("Слот не умещается в рамках семестра.")


def _active_booking_count(session: LabSession) -> int:
    return session.bookings.filter(current_status=BookingStatus.BOOKED).count()


def _create_group_bookings(session: LabSession, actor: User | None) -> int:
    """Создаёт записи GROUP для всех студентов occupant_group."""
    group = session.occupant_group
    if group is None:
        return 0
    discipline = session.lab_work.disciplines.first()
    if discipline is None:
        raise SessionError("У лабораторной работы нет дисциплин — групповая бронь невозможна.")
    students = list(
        User.objects.filter(
            role=UserRole.STUDENT,
            profile__student_group=group,
        )
    )
    if not students:
        raise SessionError("В группе-оккупанте нет студентов.")
    created = 0
    for student in students:
        booking = Booking.objects.create(
            student=student,
            lab_session=session,
            lab_work=session.lab_work,
            discipline=discipline,
            room=session.room,
            scheduled_at=session.starts_at,
            current_status=BookingStatus.BOOKED,
            registration_type=RegistrationType.GROUP,
            registered_by=actor,
        )
        BookingStatusHistory.objects.create(
            booking=booking, status=BookingStatus.BOOKED, changed_by=actor, note="Групповая бронь"
        )
        created += 1
    return created


def clean_session_payload(
    laboratory,
    data: dict,
    *,
    room: Room | None = None,
    session: LabSession | None = None,
):
    """Валидация полезной нагрузки слота.

    Возвращает словарь готовых значений. Если ``session`` передан — проверяет
    правила редактирования при активных бронях.
    """
    semester = _resolve_semester(data)
    lab_work = _resolve_lab_work(data, laboratory)
    room = room or _resolve_room(data, laboratory)
    teacher = _resolve_teacher(data)

    starts_at = _resolve_starts_at(data)

    if session is None and starts_at <= timezone.now():
        raise SessionError("Нельзя создавать слот в прошлом.")

    _validate_slot_time(starts_at, lab_work)
    ends_at = starts_at + timedelta(minutes=lab_work.duration_minutes)
    _validate_semester_bounds(starts_at, ends_at, semester)

    mode = data.get("booking_mode", LabSessionBookingMode.INDIVIDUAL)
    if mode not in LabSessionBookingMode.values:
        raise SessionError("Некорректный режим бронирования.")

    occupant_group = _resolve_occupant_group(data)
    allowed_groups = _resolve_allowed_groups(data)

    if mode == LabSessionBookingMode.GROUP:
        if occupant_group is None:
            raise SessionError("Для групповой брони укажите группу-оккупант.")
        if allowed_groups:
            raise SessionError("Групповая бронь не совместима с ограничением по группам.")
    elif occupant_group is not None:
        raise SessionError("Группа-оккупант допустима только для групповой брони.")

    _validate_schedule_conflicts(
        lab_work=lab_work,
        room=room,
        teacher=teacher,
        starts_at=starts_at,
        ends_at=ends_at,
        session=session,
    )

    # Редактирование слота с активными бронями
    if session is not None:
        booked = _active_booking_count(session)
        immutable_fields = []
        if session.lab_work_id != lab_work.pk:
            immutable_fields.append("лабораторная работа")
        if session.room_id != room.pk:
            immutable_fields.append("аудитория")
        if timezone.localtime(session.starts_at) != timezone.localtime(starts_at):
            immutable_fields.append("время начала")
        if immutable_fields and booked:
            raise SessionError(
                "Нельзя изменять {} при активных записях. Отмените слот и создайте новый.".format(
                    ", ".join(immutable_fields)
                )
            )
        if session.booking_mode != mode and booked:
            raise SessionError("Нельзя менять режим бронирования при активных записях.")
        if (
            mode == LabSessionBookingMode.GROUP
            and session.occupant_group_id
            and session.occupant_group_id != occupant_group.pk
            and booked
        ):
            raise SessionError("Нельзя менять группу-оккупанта при активных записях.")

    return {
        "lab_work": lab_work,
        "room": room,
        "semester": semester,
        "teacher": teacher,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "booking_mode": mode,
        "occupant_group": occupant_group,
        "allowed_groups": allowed_groups,
    }


@transaction.atomic
def create_session(laboratory, data: dict, *, actor: User | None = None) -> LabSession:
    payload = clean_session_payload(laboratory, data)
    session = LabSession.objects.create(
        lab_work=payload["lab_work"],
        room=payload["room"],
        semester=payload["semester"],
        teacher=payload["teacher"],
        starts_at=payload["starts_at"],
        ends_at=payload["ends_at"],
        status=LabSessionStatus.OPEN,
        booking_mode=payload["booking_mode"],
        occupant_group=payload["occupant_group"],
    )
    if payload["allowed_groups"]:
        session.allowed_student_groups.set(payload["allowed_groups"])

    LabWorkBookingRelease.objects.get_or_create(
        lab_work=session.lab_work,
        semester=session.semester,
        defaults={"booking_opens_at": timezone.now()},
    )

    if session.booking_mode == LabSessionBookingMode.GROUP:
        _create_group_bookings(session, actor)

    AuditLog.objects.create(
        actor=actor,
        action="session.create",
        entity_type="LabSession",
        entity_id=session.pk,
        payload={
            "lab_work_id": session.lab_work_id,
            "room_id": session.room_id,
            "starts_at": session.starts_at.isoformat(),
            "mode": session.booking_mode,
        },
    )
    return session


@transaction.atomic
def update_session(
    laboratory, session: LabSession, data: dict, *, actor: User | None = None
) -> LabSession:
    payload = clean_session_payload(laboratory, data, room=session.room, session=session)
    session.lab_work = payload["lab_work"]
    session.room = payload["room"]
    session.semester = payload["semester"]
    session.teacher = payload["teacher"]
    session.starts_at = payload["starts_at"]
    session.ends_at = payload["ends_at"]
    session.booking_mode = payload["booking_mode"]
    session.occupant_group = payload["occupant_group"]
    session.save()

    if payload["allowed_groups"]:
        session.allowed_student_groups.set(payload["allowed_groups"])
    else:
        session.allowed_student_groups.clear()

    if session.booking_mode == LabSessionBookingMode.GROUP and not _active_booking_count(session):
        session.bookings.filter(current_status=BookingStatus.BOOKED).delete()
        _create_group_bookings(session, actor)

    AuditLog.objects.create(
        actor=actor,
        action="session.update",
        entity_type="LabSession",
        entity_id=session.pk,
        payload={
            "lab_work_id": session.lab_work_id,
            "room_id": session.room_id,
            "starts_at": session.starts_at.isoformat(),
            "mode": session.booking_mode,
        },
    )
    return session


@transaction.atomic
def cancel_session(
    laboratory, session: LabSession, *, actor: User | None = None, note: str = ""
) -> int:
    if session.room.laboratory_id and session.room.laboratory_id != laboratory.pk:
        raise SessionError("Слот принадлежит другой лаборатории.")
    from apps.bookings.services import BookingService

    count = BookingService(actor=actor, ip_address=None).cancel_session_bookings(
        session, note=note or "Отмена слота"
    )
    AuditLog.objects.create(
        actor=actor,
        action="session.cancel",
        entity_type="LabSession",
        entity_id=session.pk,
        payload={"cancelled_bookings": count, "note": note},
    )
    return count


def update_future_session_durations(lab_work: LabWork) -> int:
    """После изменения длительности ЛР обновить ends_at будущих слотов без активных броней."""
    now = timezone.now()
    sessions = LabSession.objects.filter(
        lab_work=lab_work,
        starts_at__gt=now,
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
    ).exclude(bookings__current_status=BookingStatus.BOOKED)
    updated = 0
    for session in sessions:
        new_ends = session.starts_at + timedelta(minutes=lab_work.duration_minutes)
        if session.ends_at != new_ends:
            session.ends_at = new_ends
            session.save(update_fields=["ends_at"])
            updated += 1
    return updated
