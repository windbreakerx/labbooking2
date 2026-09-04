"""Ядро записи: создание, отмена, статусы, waitlist, неявки.

Все изменения статусов идут через ``_record_status`` (история + sticky-факты),
отмены — только через выделенные методы (cancel_source остаётся консистентным).
Посещаемость и авто-«Посетил» — в services/attendance.py.
"""

import functools
import logging
import time

from django.conf import settings
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from apps.academics.models import Discipline
from apps.academics.scope import student_can_access_lab_work, student_disciplines_qs
from apps.bookings.models import (
    NO_SHOW_EXPLANATION_THRESHOLD,
    AuditLog,
    Booking,
    BookingStatus,
    BookingStatusHistory,
    CancelSource,
    RegistrationType,
    StudentLabAttendance,
    WaitlistEntry,
)
from apps.bookings.notifications import notify_booking_event
from apps.bookings.services.session_availability import (
    booking_date_window,
    booking_window_config,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_manual_session_time_allowed,
    is_pair_time_for_booking,
    lab_work_capacity_would_be_exceeded,
    manual_booking_max_date,
    room_capacity_would_be_exceeded,
    session_matches_schedule_whitelist,
)
from apps.scheduling.models import Holiday, Laboratory, LabSession, LabSessionStatus
from apps.users.models import User, UserRole
from apps.users.roles import staff_can_modify_bookings

logger = logging.getLogger(__name__)

DEADLOCK_MAX_ATTEMPTS = 3
DEADLOCK_RETRY_BASE_SECONDS = 0.05
STAFF_NOTE_MAX_LENGTH = 500

STATUSES_WITHOUT_STAFF_NOTE = frozenset({BookingStatus.NO_SHOW, BookingStatus.REACCESS})
ACTIVE_STATUSES = {BookingStatus.BOOKED}

# Переходы change_status; отмены идут через выделенные методы.
STATUS_TRANSITIONS: dict[str, set[str]] = {
    BookingStatus.BOOKED: {BookingStatus.VISITED, BookingStatus.NO_SHOW, BookingStatus.REACCESS},
    BookingStatus.NO_SHOW: {BookingStatus.REACCESS, BookingStatus.VISITED},
}
TERMINAL_STATUSES = {
    BookingStatus.REACCESS,
    BookingStatus.VISITED,
    BookingStatus.CANCELLED,
    BookingStatus.SLOT_CANCELLED,
}


class BookingError(Exception):
    pass


def _is_deadlock_error(exc: OperationalError) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "40P01":
        return True
    cause = getattr(exc, "__cause__", None)
    if getattr(cause, "pgcode", None) == "40P01" or getattr(cause, "sqlstate", None) == "40P01":
        return True
    return "deadlock detected" in str(exc).lower()


def retry_on_deadlock(func):
    """Повторить вызов при PostgreSQL deadlock (SQLSTATE 40P01)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(1, DEADLOCK_MAX_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except OperationalError as exc:
                if not _is_deadlock_error(exc):
                    raise
                if attempt >= DEADLOCK_MAX_ATTEMPTS:
                    raise BookingError(
                        "Система обрабатывает параллельные записи. "
                        "Повторите попытку через несколько секунд."
                    ) from exc
                logger.warning(
                    "Deadlock in %s, retry %s/%s", func.__name__, attempt, DEADLOCK_MAX_ATTEMPTS
                )
                time.sleep(DEADLOCK_RETRY_BASE_SECONDS * attempt)

    return wrapper


class BookingService:
    def __init__(self, actor: User | None = None, ip_address: str | None = None):
        self.actor = actor
        self.ip_address = ip_address
        self.booking_warnings: list[str] = []

    def _log_audit(self, action: str, entity_type: str, entity_id: int, payload: dict | None = None):
        AuditLog.objects.create(
            actor=self.actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=self.ip_address,
            payload=payload or {},
        )

    def _require_staff_note(self, status: str, note: str) -> str:
        cleaned = (note or "").strip()
        if not (
            self.actor
            and self.actor.role != UserRole.STUDENT
            and staff_can_modify_bookings(self.actor)
        ):
            return cleaned
        if status in STATUSES_WITHOUT_STAFF_NOTE:
            return cleaned
        if not cleaned:
            raise BookingError("Укажите причину изменения статуса.")
        if len(cleaned) > STAFF_NOTE_MAX_LENGTH:
            raise BookingError(f"Комментарий не должен превышать {STAFF_NOTE_MAX_LENGTH} символов.")
        return cleaned

    def _notify(self, booking: Booking, event: str):
        notify_booking_event(booking, event)

    def _record_status(
        self,
        booking: Booking,
        status: str,
        note: str = "",
        *,
        clear_no_show_fact: bool = False,
        cancel_source: str | None = None,
    ):
        update_fields = ["current_status", "updated_at"]
        booking.current_status = status
        # Sticky-факт неявки: переживает REACCESS, снимается исправлением NO_SHOW → VISITED.
        if status == BookingStatus.NO_SHOW and not booking.had_no_show:
            booking.had_no_show = True
            update_fields.append("had_no_show")
        elif clear_no_show_fact and booking.had_no_show:
            booking.had_no_show = False
            update_fields.append("had_no_show")
        if status == BookingStatus.CANCELLED:
            if cancel_source:
                booking.cancel_source = cancel_source
                update_fields.append("cancel_source")
        elif booking.cancel_source is not None:
            booking.cancel_source = None
            update_fields.append("cancel_source")
        booking.save(update_fields=update_fields)
        BookingStatusHistory.objects.create(
            booking=booking, status=status, changed_by=self.actor, note=note
        )

    # --- Окна записи ---------------------------------------------------------

    def _assert_slot_bookable(self, session: LabSession, session_local_date):
        if not is_pair_time_for_booking(session.starts_at):
            raise BookingError("Запись доступна только на университетские пары.")
        if session.status != LabSessionStatus.OPEN:
            raise BookingError("Слот недоступен для записи.")
        if Holiday.objects.filter(date=session_local_date).exists():
            raise BookingError("Запись в праздничный день недоступна.")

    def _validate_booking_window(
        self,
        session: LabSession,
        skip_student_rules: bool = False,
        *,
        student: User | None = None,
    ):
        now = timezone.now()
        session_local_date = timezone.localtime(session.starts_at).date()
        laboratory_id = session.room.laboratory_id
        if not skip_student_rules:
            if not is_day_open_for_booking(session_local_date, now, laboratory_id=laboratory_id):
                min_date, max_date = booking_date_window(now, laboratory_id=laboratory_id)
                raise BookingError(
                    f"Запись сейчас открыта на даты с {min_date:%d.%m.%Y} по {max_date:%d.%m.%Y}. "
                    "Если у лаборатории включено ограничение по времени, "
                    "запись на следующий день закрывается заранее."
                )
            if not is_before_restriction_deadline(session.starts_at, now, laboratory_id=laboratory_id):
                raise BookingError("Время записи на выбранный день уже истекло.")
            if not session_matches_schedule_whitelist(session, student=student):
                raise BookingError("Слот недоступен для записи.")
        if session.starts_at <= now:
            raise BookingError("Нельзя записаться на прошедший слот.")
        self._assert_slot_bookable(session, session_local_date)

    def _validate_manual_booking_window(self, session: LabSession):
        now = timezone.now()
        if not is_manual_session_time_allowed(session, now):
            raise BookingError("Нельзя записаться на прошедший слот.")
        session_local_date = timezone.localtime(session.starts_at).date()
        max_date = manual_booking_max_date(now)
        if session_local_date > max_date:
            raise BookingError(
                f"Ручная запись доступна не далее чем на {settings.MANUAL_BOOKING_WORKING_WEEKS} "
                f"рабочие недели (до {max_date:%d.%m.%Y})."
            )
        self._assert_slot_bookable(session, session_local_date)

    # --- Пересечения и блокировки --------------------------------------------

    def _student_overlap_bookings(self, student: User, session: LabSession):
        return Booking.objects.filter(
            student=student,
            current_status__in=ACTIVE_STATUSES,
            lab_session__starts_at__lt=session.ends_at,
            lab_session__ends_at__gt=session.starts_at,
        ).select_related("lab_work", "lab_session")

    def _overlap_warning_message(self, student: User, session: LabSession) -> str:
        parts = []
        for booking in self._student_overlap_bookings(student, session):
            local = timezone.localtime(booking.scheduled_at)
            parts.append(f"«{booking.lab_work.title}» {local:%d.%m.%Y} в {local:%H:%M}")
        return (
            f"У студента уже есть запись на пересекающееся время: {'; '.join(parts)}. "
            "Согласуйте время со студентом."
        )

    def _lock_session_rows(self, session_ids: list[int] | set[int]):
        unique_sorted = sorted(set(session_ids))
        if not unique_sorted:
            return
        list(
            LabSession.objects.select_for_update(of=("self",))
            .filter(pk__in=unique_sorted)
            .order_by("pk")
            .values_list("pk", flat=True)
        )

    def _collect_booking_lock_ids(self, session: LabSession) -> list[int]:
        """Целевой слот + пересекающиеся слоты аудитории/ЛР/стенда, отсортировано (анти-deadlock)."""
        lock_ids = {session.pk}
        overlapping = LabSession.objects.filter(
            starts_at__lt=session.ends_at,
            ends_at__gt=session.starts_at,
        ).values_list("pk", flat=True)
        lock_ids.update(overlapping.filter(room_id=session.room_id))
        lock_ids.update(overlapping.filter(lab_work_id=session.lab_work_id))
        stand_id = session.lab_work.primary_stand_id
        if stand_id:
            lock_ids.update(overlapping.filter(lab_work__primary_stand_id=stand_id))
        return sorted(lock_ids)

    def _lock_for_booking(self, session: LabSession):
        self._lock_session_rows(self._collect_booking_lock_ids(session))

    def _validate_cancel_window(self, booking: Booking, by_staff: bool = False):
        if by_staff:
            return
        cancel_hours = booking_window_config(booking.room.laboratory_id)["cancel_hours"]
        deadline = booking.scheduled_at - timezone.timedelta(hours=cancel_hours)
        if timezone.now() > deadline:
            raise BookingError(f"Отмена возможна не позднее чем за {cancel_hours} часа до начала.")

    # --- Неявки (учёт в рамках change_status) ---------------------------------

    def _resolve_booking_laboratory(self, booking: Booking) -> Laboratory | None:
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

    def _get_or_create_lab_attendance(self, student: User, laboratory: Laboratory) -> StudentLabAttendance:
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

    def _adjust_no_show_count(self, student: User, laboratory: Laboratory | None, delta: int) -> bool:
        """Счётчик неявок по лаборатории + флаг объяснительной.

        Returns True, если флаг «объяснительная» только что стал обязательным.
        """
        if delta == 0 or laboratory is None:
            return False
        attendance = self._get_or_create_lab_attendance(student, laboratory)
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

    def has_had_no_show_for_lab_work(self, student: User, lab_work_id: int) -> bool:
        return Booking.objects.filter(
            student=student, lab_work_id=lab_work_id, had_no_show=True
        ).exists()

    # --- Создание записи -------------------------------------------------------

    def _check_discipline_limit(self, student: User, discipline_id: int, lab_work_id: int):
        if Booking.objects.filter(
            student=student,
            discipline_id=discipline_id,
            current_status__in=ACTIVE_STATUSES,
        ).exists():
            raise BookingError("Уже есть активная запись по этой дисциплине.")
        if Booking.objects.filter(
            student=student,
            lab_work_id=lab_work_id,
            current_status=BookingStatus.VISITED,
        ).exists():
            raise BookingError("Вы уже посетили эту лабораторную работу.")

    def create_booking(
        self,
        student: User,
        session_id: int,
        *,
        discipline_id: int | None = None,
        manual: bool = False,
        skip_student_rules: bool = False,
    ) -> Booking:
        self.booking_warnings = []
        return self._create_booking_with_retry(
            student,
            session_id,
            discipline_id=discipline_id,
            manual=manual,
            skip_student_rules=skip_student_rules,
        )

    @retry_on_deadlock
    def _create_booking_with_retry(self, student, session_id, **kwargs) -> Booking:
        with transaction.atomic():
            return self._create_booking_in_transaction(student, session_id, **kwargs)

    def _resolve_booking_discipline(
        self,
        student: User,
        lab_work_id: int,
        discipline_id: int | None,
        *,
        manual: bool = False,
        skip_student_rules: bool = False,
    ) -> Discipline:
        student_rules_apply = student.role == UserRole.STUDENT and (manual or not skip_student_rules)
        if discipline_id is not None:
            discipline = Discipline.objects.filter(lab_works=lab_work_id, pk=discipline_id).first()
            if discipline is None:
                raise BookingError("Дисциплина недоступна для этой лабораторной работы.")
            if student_rules_apply and not student_disciplines_qs(student).filter(pk=discipline_id).exists():
                raise BookingError("Дисциплина недоступна для этой лабораторной работы.")
            return discipline
        if skip_student_rules and not manual:
            discipline = Discipline.objects.filter(lab_works=lab_work_id).order_by("title").first()
            if discipline is None:
                raise BookingError("У лабораторной работы нет привязанных дисциплин.")
            return discipline
        discipline = student_disciplines_qs(student).filter(lab_works=lab_work_id).order_by("title").first()
        if discipline is None:
            raise BookingError("Дисциплина недоступна для этой лабораторной работы.")
        return discipline

    def _create_booking_in_transaction(
        self,
        student: User,
        session_id: int,
        *,
        discipline_id: int | None = None,
        manual: bool = False,
        skip_student_rules: bool = False,
    ) -> Booking:
        if manual and self.actor and not staff_can_modify_bookings(self.actor):
            raise BookingError("Недостаточно прав для ручной записи.")
        session = (
            LabSession.objects.select_related("lab_work", "room")
            .prefetch_related("lab_work__disciplines")
            .get(pk=session_id)
        )
        student_rules_apply = student.role == UserRole.STUDENT and (manual or not skip_student_rules)
        if student_rules_apply and not student_can_access_lab_work(student, session.lab_work_id):
            raise BookingError("Лабораторная работа недоступна для вашей группы.")
        booking_discipline = self._resolve_booking_discipline(
            student,
            session.lab_work_id,
            discipline_id,
            manual=manual,
            skip_student_rules=skip_student_rules,
        )
        self._lock_for_booking(session)
        if manual:
            self._validate_manual_booking_window(session)
        else:
            self._validate_booking_window(
                session, skip_student_rules=skip_student_rules, student=student
            )

        if manual:
            self._validate_manual_booking_rules(student, session)
        elif not skip_student_rules:
            self._check_discipline_limit(student, booking_discipline.pk, session.lab_work_id)
            if self._student_overlap_bookings(student, session).exists():
                raise BookingError("У вас уже есть запись на пересекающееся время.")
            self._enforce_capacity_limits(session)

        booking = Booking.objects.create(
            student=student,
            lab_session=session,
            lab_work=session.lab_work,
            discipline=booking_discipline,
            room=session.room,
            scheduled_at=session.starts_at,
            current_status=BookingStatus.BOOKED,
            registration_type=RegistrationType.MANUAL if manual else RegistrationType.AUTO,
            registered_by=self.actor if manual else None,
        )
        self._record_status(booking, BookingStatus.BOOKED, "Создание записи")
        self._log_audit("booking.create", "Booking", booking.pk, {"session_id": session_id})
        self._notify(booking, "booked")
        return booking

    def _validate_manual_booking_rules(self, student: User, session: LabSession):
        """Ручная запись: лимиты игнорируются, стенд и пересечения — предупреждения."""
        if session.is_stand_blocked_by_other_lab_work():
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")
        if self._student_overlap_bookings(student, session).exists():
            self.booking_warnings.append(self._overlap_warning_message(student, session))

    def _enforce_capacity_limits(self, session: LabSession):
        booked_count = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
        if booked_count >= session.capacity:
            raise BookingError("Нет свободных мест.")
        if lab_work_capacity_would_be_exceeded(session):
            raise BookingError(
                "Лимит мест для этой лабораторной работы исчерпан на выбранный интервал. "
                "Выберите другую пару."
            )
        if room_capacity_would_be_exceeded(session):
            raise BookingError(
                f"Аудитория {session.room.number} заполнена на это время. Выберите другую пару."
            )
        if session.is_stand_blocked_by_other_lab_work():
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")

    # --- Отмены и статусы ------------------------------------------------------

    @transaction.atomic
    def cancel_booking(self, booking: Booking, by_staff: bool = False, note: str = "") -> Booking:
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if booking.current_status != BookingStatus.BOOKED:
            raise BookingError("Можно отменить только активную запись.")
        self._validate_cancel_window(booking, by_staff=by_staff)
        source = CancelSource.STAFF if by_staff else CancelSource.STUDENT
        if by_staff:
            status_note = self._require_staff_note(BookingStatus.CANCELLED, note)
        else:
            status_note = (note or "").strip() or "Отмена студентом"
        self._record_status(booking, BookingStatus.CANCELLED, status_note, cancel_source=source)
        self._log_audit("booking.cancel", "Booking", booking.pk, {"cancel_source": source})
        self._notify(booking, "cancelled")
        self._promote_waitlist(booking.lab_session)
        return booking

    @transaction.atomic
    def mark_slot_cancelled(self, booking: Booking, note: str = "") -> Booking:
        """BOOKED → SLOT_CANCELLED (отмена слота расписанием/лабораторией)."""
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("room", "lab_work", "discipline", "student", "lab_session")
            .get(pk=booking.pk)
        )
        if booking.current_status != BookingStatus.BOOKED:
            raise BookingError("Отменить слот можно только для активной записи.")
        status_note = self._require_staff_note(BookingStatus.SLOT_CANCELLED, note) or "Слот отменён"
        self._record_status(booking, BookingStatus.SLOT_CANCELLED, status_note)
        self._log_audit(
            "booking.slot_cancelled", "Booking", booking.pk, {"status": BookingStatus.SLOT_CANCELLED}
        )
        self._notify(booking, "slot_cancelled")
        self._promote_waitlist(booking.lab_session)
        return booking

    @transaction.atomic
    def change_status(self, booking: Booking, new_status: str, note: str = "") -> Booking:
        if self.actor and not staff_can_modify_bookings(self.actor):
            raise BookingError("Недостаточно прав для изменения статуса записи.")
        # of=("self",): PostgreSQL запрещает FOR UPDATE на nullable outer joins.
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("room", "lab_work", "discipline", "student")
            .get(pk=booking.pk)
        )
        if new_status == booking.current_status:
            return booking
        if booking.current_status in TERMINAL_STATUSES:
            raise BookingError(
                f"Статус «{booking.get_current_status_display()}» конечный и не может быть изменён."
            )
        # Отмены человеком — только через cancel_booking (он ставит cancel_source).
        if new_status == BookingStatus.CANCELLED:
            if booking.current_status != BookingStatus.BOOKED:
                raise BookingError("Отменить можно только активную запись.")
            return self.cancel_booking(booking, by_staff=True, note=note)
        if new_status == BookingStatus.SLOT_CANCELLED:
            if booking.current_status != BookingStatus.BOOKED:
                raise BookingError("Отменить слот можно только для активной записи.")
            return self.mark_slot_cancelled(booking, note=note)
        if new_status not in STATUS_TRANSITIONS.get(booking.current_status, set()):
            raise BookingError(f"Переход {booking.current_status} → {new_status} недопустим.")

        status_note = self._require_staff_note(new_status, note)
        laboratory = self._resolve_booking_laboratory(booking)
        correcting_no_show = (
            new_status == BookingStatus.VISITED
            and booking.current_status == BookingStatus.NO_SHOW
            and booking.had_no_show
        )
        explanation_just_required = False
        if new_status == BookingStatus.NO_SHOW and not booking.had_no_show:
            explanation_just_required = self._adjust_no_show_count(booking.student, laboratory, 1)
        elif correcting_no_show:
            self._adjust_no_show_count(booking.student, laboratory, -1)

        self._record_status(booking, new_status, status_note, clear_no_show_fact=correcting_no_show)
        self._log_audit("booking.status_change", "Booking", booking.pk, {"status": new_status})
        notify_map = {
            BookingStatus.NO_SHOW: "no_show",
            BookingStatus.REACCESS: "reaccess",
            BookingStatus.VISITED: "visited",
        }
        if new_status in notify_map:
            self._notify(booking, notify_map[new_status])
        if explanation_just_required:
            self._notify(booking, "explanation_required")
        return booking

    # --- Waitlist и массовые операции ------------------------------------------

    def _promote_waitlist(self, session: LabSession):
        entry = (
            WaitlistEntry.objects.filter(lab_session=session)
            .order_by("position")
            .select_related("student")
            .first()
        )
        if not entry:
            return
        try:
            self.create_booking(entry.student, session.pk)
            entry.delete()
        except BookingError:
            entry.delete()

    @transaction.atomic
    def join_waitlist(self, student: User, session_id: int) -> WaitlistEntry:
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
        entry = WaitlistEntry.objects.create(
            lab_session=session, student=student, position=position
        )
        self._log_audit("waitlist.join", "WaitlistEntry", entry.pk)
        return entry

    @transaction.atomic
    def cancel_session_bookings(self, session: LabSession, note: str = "") -> int:
        """Отмена слота: активные записи → SLOT_CANCELLED (без REACCESS)."""
        session.status = LabSessionStatus.CANCELLED
        session.save(update_fields=["status"])
        count = 0
        for booking in session.bookings.filter(current_status=BookingStatus.BOOKED):
            self.mark_slot_cancelled(booking, note=note or "Изменение расписания")
            count += 1
        self._log_audit(
            "session.cancel", "LabSession", session.pk, {"slot_cancelled_count": count}
        )
        return count
