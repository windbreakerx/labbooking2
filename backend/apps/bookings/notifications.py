"""Уведомления о записях: единая точка формирования текстов и хук отправки.

Транспорт — заглушка (лог). MAX-бот после пилота подключается здесь,
не трогая сервисы (REBUILD_PLAN §9).
"""

import logging

from django.utils import timezone

from apps.bookings.models import NO_SHOW_EXPLANATION_THRESHOLD, Booking

logger = logging.getLogger(__name__)


def booking_notification_payload(booking: Booking, event: str) -> tuple[str, str]:
    """(Заголовок, текст) уведомления студенту о событии записи."""
    local = timezone.localtime(booking.scheduled_at)
    slot = f"{local:%d.%m.%Y} в {local:%H:%M}"
    work = booking.lab_work.title
    discipline = booking.discipline.title
    room = booking.room.number
    templates = {
        "booked": (
            "Запись на лабораторную работу",
            f"Вы записаны на «{work}» по дисциплине «{discipline}» {slot} в ауд. №{room}.",
        ),
        "cancelled": (
            "Отмена записи",
            f"Вы отменили запись на «{work}» по дисциплине «{discipline}» {slot}.",
        ),
        "slot_cancelled": (
            "Занятие отменено",
            f"Занятие по «{work}» ({slot}, ауд. №{room}) отменено лабораторией. "
            "Запишитесь на другой слот.",
        ),
        "visited": (
            "Посещение засчитано",
            f"Вы посетили «{work}» по дисциплине «{discipline}». "
            "Можете записаться на другие работы по дисциплине.",
        ),
        "no_show": (
            "Неявка засчитана",
            f"Вы не посетили «{work}». При {NO_SHOW_EXPLANATION_THRESHOLD} неявках "
            "в лаборатории потребуется объяснительная записка.",
        ),
        "explanation_required": (
            "Требуется объяснительная записка",
            f"У вас {booking.student.lab_attendance.filter(explanation_required=True).count()} "
            f"лабораторий с {NO_SHOW_EXPLANATION_THRESHOLD}+ неявками. "
            "Обратитесь в лабораторию с объяснительной запиской.",
        ),
        "reaccess": (
            "Повторный доступ к записи",
            f"Вам открыт повторный доступ к «{work}» по дисциплине «{discipline}». "
            "Запишитесь на другой слот для посещения или доделывания работы.",
        ),
    }
    return templates.get(event, ("Уведомление", ""))


def notify_booking_event(booking: Booking, event: str) -> None:
    """Хук вызывается сервисами; доставка подключается здесь (сейчас — лог)."""
    title, body = booking_notification_payload(booking, event)
    logger.info(
        "booking notify: booking=%s student=%s event=%s — %s",
        booking.pk,
        booking.student_id,
        event,
        title,
    )


def waitlist_notification_payload(entry, event: str) -> tuple[str, str]:
    """(Заголовок, текст) уведомления участнику очереди на слот."""
    session = entry.lab_session
    local = timezone.localtime(session.starts_at)
    slot = f"{local:%d.%m.%Y} в {local:%H:%M}"
    work = session.lab_work.title
    room = session.room.number
    templates = {
        "dropped": (
            "Вы выбыли из очереди",
            f"Место освободилось на «{work}» ({slot}, ауд. №{room}), но записаться не удалось: "
            "действуют правила записи. Выберите другой слот.",
        ),
        "session_cancelled": (
            "Слот отменён — очередь сброшена",
            f"Слот «{work}» ({slot}, ауд. №{room}) отменён, очередь на него сброшена. "
            "Запишитесь на другой слот.",
        ),
    }
    return templates.get(event, ("Уведомление", ""))


def notify_waitlist_event(entry, event: str) -> None:
    """Хук событий очереди (выбытие, сброс при отмене слота)."""
    title, body = waitlist_notification_payload(entry, event)
    logger.info(
        "waitlist notify: entry=%s student=%s event=%s — %s",
        entry.pk,
        entry.student_id,
        event,
        title,
    )
