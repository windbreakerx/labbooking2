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
from apps.bookings.scope import staff_can_modify_room
from apps.bookings.services.booking_mutations import BookingMutationsMixin
from apps.bookings.services.errors import BookingError
from apps.bookings.services.retry import retry_on_deadlock
from apps.bookings.services.seat_capacity import seat_remainings, session_seat_counts
from apps.bookings.services.session_availability import (
    can_student_book_session,
    is_booking_release_open,
    is_manual_session_time_allowed,
    is_pair_time_for_booking,
    manual_booking_max_date,
)
from apps.scheduling.models import Holiday, LabSession, LabSessionBookingMode, LabSessionStatus
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
        if session.booking_mode == LabSessionBookingMode.GROUP:
            raise BookingError("Групповой слот недоступен для самостоятельной записи.")
        if not is_pair_time_for_booking(session.starts_at):
            raise BookingError("Запись доступна только на университетские пары.")
        if session.status != LabSessionStatus.OPEN:
            raise BookingError("Слот недоступен для записи.")
        if Holiday.objects.filter(date=session_local_date).exists():
            raise BookingError("Запись в праздничный день недоступна.")

    def _validate_booking_window(
        self,
        session: LabSession,
        *,
        student: User | None = None,
    ):
        now = timezone.now()
        session_local_date = timezone.localtime(session.starts_at).date()
        if not is_booking_release_open(session.lab_work_id, session.semester_id, now):
            raise BookingError("запись на эту лабораторную работу в текущем семестре закрыта")
        if not can_student_book_session(student, session, now):
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
        """Целевой слот + пересекающиеся слоты этой же аудитории (анти-deadlock: по возрастанию pk).

        Места теперь считаются только внутри слота и комнаты, поэтому чужие
        аудитории и параллельные слоты той же ЛР блокировать незачем.
        """
        lock_ids = {session.pk}
        overlapping = LabSession.objects.filter(
            starts_at__lt=session.ends_at,
            ends_at__gt=session.starts_at,
        ).values_list("pk", flat=True)
        lock_ids.update(overlapping.filter(room_id=session.room_id))
        return sorted(lock_ids)

    def _lock_for_booking(self, session: LabSession):
        self._lock_session_rows(self._collect_booking_lock_ids(session))

    # --- Создание записи -------------------------------------------------------

    def _check_lab_work_and_discipline_limits(self, student: User, discipline_id: int, lab_work):
        visited_count = Booking.objects.filter(
            student=student,
            lab_work=lab_work,
            current_status=BookingStatus.VISITED,
        ).count()
        if visited_count >= lab_work.required_sessions:
            raise BookingError("Лабораторная работа уже выполнена.")
        active_count = Booking.objects.filter(
            student=student,
            lab_work=lab_work,
            current_status__in=ACTIVE_STATUSES,
        ).count()
        if active_count >= lab_work.required_sessions:
            raise BookingError("Достигнут лимит активных записей на эту лабораторную работу.")

    def create_booking(
        self,
        student: User,
        session_id: int,
        *,
        discipline_id: int | None = None,
        manual: bool = False,
    ) -> Booking:
        self.booking_warnings = []
        return self._create_booking_with_retry(
            student,
            session_id,
            discipline_id=discipline_id,
            manual=manual,
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
    ) -> Discipline:
        student_rules_apply = student.role == UserRole.STUDENT
        if discipline_id is not None:
            discipline = Discipline.objects.filter(lab_works=lab_work_id, pk=discipline_id).first()
            if discipline is None:
                raise BookingError("Дисциплина недоступна для этой лабораторной работы.")
            if student_rules_apply and not student_disciplines_qs(student).filter(pk=discipline_id).exists():
                raise BookingError("Дисциплина недоступна для этой лабораторной работы.")
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
    ) -> Booking:
        if manual and self.actor and not staff_can_modify_bookings(self.actor):
            raise BookingError("Недостаточно прав для ручной записи.")
        session = (
            LabSession.objects.select_related("lab_work", "room")
            .prefetch_related("lab_work__disciplines")
            .get(pk=session_id)
        )
        if (
            manual
            and self.actor
            and staff_can_modify_bookings(self.actor)
            and not staff_can_modify_room(self.actor, session.room)
        ):
            raise BookingError(
                "Нет прав на запись в эту аудиторию. Обратитесь к завлабу."
            )
        student_rules_apply = student.role == UserRole.STUDENT
        if student_rules_apply and not student_can_access_lab_work(student, session.lab_work_id):
            raise BookingError("Лабораторная работа недоступна для вашей группы.")
        booking_discipline = self._resolve_booking_discipline(
            student,
            session.lab_work_id,
            discipline_id,
        )
        self._lock_for_booking(session)
        if manual:
            self._validate_manual_booking_window(session)
        else:
            self._validate_booking_window(session, student=student)

        if manual:
            self._validate_manual_booking_rules(student, session)
        else:
            self._check_lab_work_and_discipline_limits(student, booking_discipline.pk, session.lab_work)
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
        if manual:
            self._log_manual_overflow(booking, session)
        self._notify(booking, "booked")
        return booking

    def _log_manual_overflow(self, booking: Booking, session: LabSession) -> None:
        """Зафиксировать ручную запись сверх лимита слота или аудитории."""
        session_booked, room_other, _ = session_seat_counts(session)
        room_total = room_other + session_booked
        payload = {
            "session_id": session.pk,
            "session_booked": session_booked,
            "session_capacity": session.capacity,
            "room_total": room_total,
            "room_capacity": session.room.capacity,
            "student_id": booking.student_id,
        }
        if session_booked > session.capacity or room_total > session.room.capacity:
            self._log_audit("booking.overflow", "Booking", booking.pk, payload)

    def _validate_manual_booking_rules(self, student: User, session: LabSession):
        """Ручная запись: жёстко блокируется только стенд, остальное — предупреждения.

        Вместимость слота равна вместимости ЛР, поэтому отдельно про слот не
        предупреждаем: единственное осмысленное ограничение — аудитория.
        Факт превышения пишется в аудит при создании записи.
        """
        if session.is_stand_blocked():
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")
        if self._student_overlap_bookings(student, session).exists():
            self.booking_warnings.append(self._overlap_warning_message(student, session))
        session_booked, room_other, _ = session_seat_counts(session)
        if room_other + session_booked >= session.room.capacity:
            self.booking_warnings.append(
                f"Аудитория {session.room.number} заполнена "
                f"({room_other + session_booked}/{session.room.capacity}) — запись сверх лимита."
            )

    def _enforce_capacity_limits(self, session: LabSession):
        session_booked, room_other, stand_blocked = session_seat_counts(session)
        session_remaining, room_remaining = seat_remainings(
            session_capacity=session.capacity,
            session_booked=session_booked,
            room_capacity=session.room.capacity,
            room_other_booked=room_other,
        )
        if session_remaining <= 0:
            raise BookingError("Нет свободных мест.")
        if room_remaining <= 0:
            raise BookingError(
                f"Аудитория {session.room.number} заполнена на это время. Выберите другую пару."
            )
        if stand_blocked:
            raise BookingError("Стенд уже занят на это время. Выберите другой интервал.")
