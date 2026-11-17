"""Фоновые задачи бронирований.

Запускаются через django-q2. Все задачи должны быть реентерабельными
и не полагаться на request/HttpRequest.
"""

import logging

from apps.bookings.services.attendance import mark_visited_for_ended_sessions

logger = logging.getLogger(__name__)


def mark_visited_task() -> int:
    """Проставить статус VISITED завершённым слотам по настройкам лабораторий.

    Запускается ежедневно в 20:00 МСК через django-q2 Schedule.
    """
    count = mark_visited_for_ended_sessions()
    logger.info("mark_visited_task processed %s booking(s)", count)
    return count
