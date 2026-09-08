"""Отмены и смены статусов записи — миксин BookingService.

Публичные методы несут ретрай deadlock на верхнем уровне; внутренние переходы
(change_status → отмена) зовут ``*_impl``-методы напрямую, без вложенных
ретраев. Промоушен очереди планируется после коммита (waitlist).
"""

from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, BookingStatus, CancelSource
from apps.bookings.services.attendance import adjust_no_show_count, resolve_booking_laboratory
from apps.bookings.services.errors import BookingError
from apps.bookings.services.retry import retry_on_deadlock
from apps.bookings.services.session_availability import booking_window_config
from apps.scheduling.models import LabSessionStatus
from apps.users.models import UserRole
from apps.users.roles import staff_can_modify_bookings

STAFF_NOTE_MAX_LENGTH = 500
STATUSES_WITHOUT_STAFF_NOTE = frozenset({BookingStatus.NO_SHOW, BookingStatus.REACCESS})
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


def _schedule_promotion(session_id: int) -> None:
    from apps.bookings.services.waitlist import schedule_waitlist_promotion

    schedule_waitlist_promotion(session_id)


class BookingMutationsMixin:
    """Зависит от BookingService: actor, _record_status, _log_audit, _notify."""

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

    def _validate_cancel_window(self, booking: Booking, by_staff: bool = False):
        if by_staff:
            return
        cancel_hours = booking_window_config(booking.room.laboratory_id)["cancel_hours"]
        deadline = booking.scheduled_at - timezone.timedelta(hours=cancel_hours)
        if timezone.now() > deadline:
            raise BookingError(f"Отмена возможна не позднее чем за {cancel_hours} часа до начала.")

    # --- Отмена записью --------------------------------------------------------

    def cancel_booking(self, booking: Booking, by_staff: bool = False, note: str = "") -> Booking:
        return self._cancel_booking_with_retry(booking, by_staff=by_staff, note=note)

    @retry_on_deadlock
    def _cancel_booking_with_retry(self, *args, **kwargs) -> Booking:
        with transaction.atomic():
            return self._cancel_booking_impl(*args, **kwargs)

    def _cancel_booking_impl(self, booking: Booking, by_staff: bool, note: str) -> Booking:
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
        _schedule_promotion(booking.lab_session_id)
        return booking

    # --- Отмена слота ----------------------------------------------------------

    def mark_slot_cancelled(self, booking: Booking, note: str = "") -> Booking:
        """BOOKED → SLOT_CANCELLED (отмена слота расписанием/лабораторией)."""
        return self._mark_slot_cancelled_with_retry(booking, note=note)

    @retry_on_deadlock
    def _mark_slot_cancelled_with_retry(self, *args, **kwargs) -> Booking:
        with transaction.atomic():
            return self._mark_slot_cancelled_impl(*args, **kwargs)

    def _mark_slot_cancelled_impl(self, booking: Booking, note: str) -> Booking:
        # of=("self",): PostgreSQL запрещает FOR UPDATE на nullable outer joins.
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
        _schedule_promotion(booking.lab_session_id)
        return booking

    # --- Смена статуса ---------------------------------------------------------

    def change_status(self, booking: Booking, new_status: str, note: str = "") -> Booking:
        return self._change_status_with_retry(booking, new_status, note=note)

    @retry_on_deadlock
    def _change_status_with_retry(self, *args, **kwargs) -> Booking:
        with transaction.atomic():
            return self._change_status_impl(*args, **kwargs)

    def _change_status_impl(self, booking: Booking, new_status: str, note: str) -> Booking:
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
            return self._cancel_booking_impl(booking, by_staff=True, note=note)
        if new_status == BookingStatus.SLOT_CANCELLED:
            if booking.current_status != BookingStatus.BOOKED:
                raise BookingError("Отменить слот можно только для активной записи.")
            return self._mark_slot_cancelled_impl(booking, note=note)
        if new_status not in STATUS_TRANSITIONS.get(booking.current_status, set()):
            raise BookingError(f"Переход {booking.current_status} → {new_status} недопустим.")

        status_note = self._require_staff_note(new_status, note)
        laboratory = resolve_booking_laboratory(booking)
        correcting_no_show = (
            new_status == BookingStatus.VISITED
            and booking.current_status == BookingStatus.NO_SHOW
            and booking.had_no_show
        )
        explanation_just_required = False
        if new_status == BookingStatus.NO_SHOW and not booking.had_no_show:
            explanation_just_required = adjust_no_show_count(booking.student, laboratory, 1)
        elif correcting_no_show:
            adjust_no_show_count(booking.student, laboratory, -1)

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

    # --- Массовая отмена слота ---------------------------------------------------

    def cancel_session_bookings(self, session, note: str = "") -> int:
        """Отмена слота: активные записи → SLOT_CANCELLED, очередь выкидывается."""
        return self._cancel_session_bookings_with_retry(session, note=note)

    @retry_on_deadlock
    def _cancel_session_bookings_with_retry(self, *args, **kwargs) -> int:
        with transaction.atomic():
            return self._cancel_session_bookings_impl(*args, **kwargs)

    def _cancel_session_bookings_impl(self, session, note: str) -> int:
        from apps.bookings.services.waitlist import drop_session_waitlist

        session.status = LabSessionStatus.CANCELLED
        session.save(update_fields=["status"])
        dropped = drop_session_waitlist(session)
        count = 0
        for booking in session.bookings.filter(current_status=BookingStatus.BOOKED):
            self._mark_slot_cancelled_impl(booking, note=note or "Изменение расписания")
            count += 1
        self._log_audit(
            "session.cancel",
            "LabSession",
            session.pk,
            {"slot_cancelled_count": count, "waitlist_dropped": dropped},
        )
        return count
