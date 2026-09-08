"""Ядро записи: создание (студент/ручное), окна записи, локи, лимиты.

Отмены и смены статусов — BookingMutationsMixin (booking_mutations.py);
очередь — waitlist.py; счётчики неявок — attendance.py.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.academics.models import Discipline
from apps.academics.scope import student_can_access_lab_work, student_disciplines_qs
from apps.bookings.models import (
    AuditLog,
    Booking,
    BookingStatus,
    BookingStatusHistory,
    RegistrationType,
)
from apps.bookings.notifications import notify_booking_event
from apps.bookings.services.booking_mutations import BookingMutationsMixin
from apps.bookings.services.errors import BookingError
from apps.bookings.services.retry import retry_on_deadlock
from apps.bookings.services.seat_capacity import seat_remainings, session_seat_counts
from apps.bookings.services.session_availability import (
    booking_date_window,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_manual_session_time_allowed,
    is_pair_time_for_booking,
    manual_booking_max_date,
    session_matches_schedule_whitelist,
)
from apps.scheduling.models import Holiday, LabSession, LabSessionStatus
from apps.users.models import User, UserRole
from apps.users.roles import staff_can_modify_bookings

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {BookingStatus.BOOKED}


class BookingService(BookingMutationsMixin):
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
        """Ручная запись: лимиты игнорируются, стенд — жёстко, остальное — предупреждения."""
        if session.is_stand_blocked_by_other_lab_work():
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")
        if self._student_overlap_bookings(student, session).exists():
            self.booking_warnings.append(self._overlap_warning_message(student, session))
        booked_count = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
        if booked_count >= session.capacity:
            self.booking_warnings.append(
                f"Слот заполнен ({booked_count}/{session.capacity}) — запись сверх лимита."
            )

    def _enforce_capacity_limits(self, session: LabSession):
        session_booked, lab_other, room_other, stand_blocked = session_seat_counts(session)
        session_remaining, lab_remaining, room_remaining = seat_remainings(
            session_capacity=session.capacity,
            session_booked=session_booked,
            lab_work_capacity=session.lab_work.capacity,
            lab_other_booked=lab_other,
            room_capacity=session.room.capacity,
            room_other_booked=room_other,
        )
        if session_remaining <= 0:
            raise BookingError("Нет свободных мест.")
        if lab_remaining <= 0:
            raise BookingError(
                "Лимит мест для этой лабораторной работы исчерпан на выбранный интервал. "
                "Выберите другую пару."
            )
        if room_remaining is not None and room_remaining <= 0:
            raise BookingError(
                f"Аудитория {session.room.number} заполнена на это время. Выберите другую пару."
            )
        if stand_blocked:
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")
