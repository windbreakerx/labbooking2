"""Единая математика мест: остатки слота/ЛР/аудитории, стенд.

Формулы живут здесь в одном месте — их используют жёсткие проверки
BookingService, ``LabSession.available_seats`` и векторизованный каталог
(session_catalog). Скалярные счётчики — для путей брони (один слот);
каталог считает те же величины оптом.
"""

from apps.scheduling.models import LabSession


def seat_remainings(
    *,
    session_capacity: int,
    session_booked: int,
    lab_work_capacity: int,
    lab_other_booked: int,
    room_capacity: int,
    room_other_booked: int,
) -> tuple[int, int, int | None]:
    """Остатки мест: (слот, ЛР, аудитория|None).

    Остаток аудитории — None, пока в ней нет записей на пересекающихся
    чужих слотах: такая аудитория слот не ограничивает (семантика v1).
    """
    session_remaining = session_capacity - session_booked
    lab_remaining = lab_work_capacity - lab_other_booked - session_booked
    room_remaining = (
        room_capacity - room_other_booked - session_booked if room_other_booked else None
    )
    return session_remaining, lab_remaining, room_remaining


def seats_available(
    *,
    session_capacity: int,
    session_booked: int,
    lab_work_capacity: int,
    lab_other_booked: int,
    room_capacity: int,
    room_other_booked: int,
    stand_blocked: bool = False,
) -> int:
    """Свободные места слота: min остатков; стенд занят — 0."""
    if stand_blocked:
        return 0
    session_remaining, lab_remaining, room_remaining = seat_remainings(
        session_capacity=session_capacity,
        session_booked=session_booked,
        lab_work_capacity=lab_work_capacity,
        lab_other_booked=lab_other_booked,
        room_capacity=room_capacity,
        room_other_booked=room_other_booked,
    )
    limits = [session_remaining, lab_remaining]
    if room_remaining is not None:
        limits.append(room_remaining)
    return max(0, min(limits))


def session_seat_counts(session: LabSession) -> tuple[int, int, int, bool]:
    """Счётчики слота: (занято, чужие по ЛР, чужие по аудитории, стенд занят).

    Чужие — записи BOOKED на пересекающихся слотах той же ЛР / той же
    аудитории; стенд — занят ли первичный стенд ЛР другой ЛР на это время.
    """
    from apps.bookings.models import Booking, BookingStatus

    session_booked = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    overlapping = Booking.objects.filter(
        current_status=BookingStatus.BOOKED,
        lab_session__starts_at__lt=session.ends_at,
        lab_session__ends_at__gt=session.starts_at,
    ).exclude(lab_session_id=session.pk)
    lab_other = overlapping.filter(lab_session__lab_work_id=session.lab_work_id).count()
    room_other = overlapping.filter(lab_session__room_id=session.room_id).count()
    stand_blocked = session.is_stand_blocked_by_other_lab_work()
    return session_booked, lab_other, room_other, stand_blocked


def session_available_seats(session: LabSession) -> int:
    """Свободные места слота (LabSession.available_seats и тесты эквивалентности)."""
    session_booked, lab_other, room_other, stand_blocked = session_seat_counts(session)
    return seats_available(
        session_capacity=session.capacity,
        session_booked=session_booked,
        lab_work_capacity=session.lab_work.capacity,
        lab_other_booked=lab_other,
        room_capacity=session.room.capacity,
        room_other_booked=room_other,
        stand_blocked=stand_blocked,
    )
