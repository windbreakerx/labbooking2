"""Повтор при PostgreSQL-deadlock (SQLSTATE 40P01).

Ретрай работает только на верхнем уровне, вне внешней atomic-транзакции:
после deadlock транзакция PG уже aborted, повтор внутри неё упадёт
с InFailedSqlTransaction, а не свежей попыткой.
"""

import functools
import logging
import time

from django.db.utils import OperationalError

from apps.bookings.services.errors import BookingError

logger = logging.getLogger(__name__)

DEADLOCK_MAX_ATTEMPTS = 3
DEADLOCK_RETRY_BASE_SECONDS = 0.05


def is_deadlock_error(exc: OperationalError) -> bool:
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
                if not is_deadlock_error(exc):
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
