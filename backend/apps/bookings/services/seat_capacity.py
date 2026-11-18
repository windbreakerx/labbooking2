"""Единая математика мест: остатки слота и аудитории, занятость стенда.

Формулы живут здесь в одном месте — их используют жёсткие проверки
BookingService, ``LabSession.available_seats`` и векторизованный каталог
(session_catalog). Скалярные счётчики — для путей брони (один слот);
каталог считает те же величины оптом.

Вместимость слота не хранится: она равна вместимости лабораторной работы
(для групповой брони — размеру группы) и приходит из ``session.capacity``.
"""

from apps.scheduling.models import LabSession


def seat_remainings(
    *,
    session_capacity: int,
    session_booked: int,
    room_capacity: int,
    room_other_booked: int,
) -> tuple[int, int]:
    """Остатки мест: (слот, аудитория).

    Аудитория ограничивает всегда: сумма записанных на пересекающиеся
    слоты комнаты не может превышать её вместимость.
    """
    session_remaining = session_capacity - session_booked
    room_remaining = room_capacity - room_other_booked - session_booked
    return session_remaining, room_remaining


def seats_available(
    *,
    session_capacity: int,
    session_booked: int,
    room_capacity: int,
    room_other_booked: int,
    stand_blocked: bool = False,
) -> int:
    """Свободные места слота: минимум из остатков; стенд занят — 0."""
    if stand_blocked:
        return 0
    session_remaining, room_remaining = seat_remainings(
        session_capacity=session_capacity,
        session_booked=session_booked,
        room_capacity=room_capacity,
        room_other_booked=room_other_booked,
    )
    return max(0, min(session_remaining, room_remaining))


def session_seat_counts(session: LabSession) -> tuple[int, int, bool]:
    """Счётчики слота: (записано, чужие записи в аудитории, стенд занят)."""
    from apps.bookings.models import Booking, BookingStatus

    session_booked = session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    room_other = (
        Booking.objects.filter(
            current_status=BookingStatus.BOOKED,
            lab_session__room_id=session.room_id,
            lab_session__starts_at__lt=session.ends_at,
            lab_session__ends_at__gt=session.starts_at,
        )
        .exclude(lab_session_id=session.pk)
        .count()
    )
    return session_booked, room_other, session.is_stand_blocked()


def session_available_seats(session: LabSession) -> int:
    """Свободные места слота (LabSession.available_seats и тесты эквивалентности)."""
    session_booked, room_other, stand_blocked = session_seat_counts(session)
    return seats_available(
        session_capacity=session.capacity,
        session_booked=session_booked,
        room_capacity=session.room.capacity,
        room_other_booked=room_other,
        stand_blocked=stand_blocked,
    )
