"""Скоуп сотрудников для записей и обращений (SYS_ADMIN видит всё).

Заменяет v1-«магию» staff_lab_filter(lookup-строки) именованными функциями.
Толерантность к данным: брони в комнатах без лаборатории остаются видимыми
в УЦ лаборатории сотрудника; брони по ЛР/дисциплине лаборатории — видимы
всегда (аудитория могла быть перепривязана после записи).
"""

from django.db.models import Q

from apps.academics.scope import resolve_staff_laboratory, resolve_staff_training_center
from apps.bookings.models import Booking, SupportTicket
from apps.scheduling.models import LabSession
from apps.users.models import UserRole

_BOOKING_RELATED = ("student", "lab_work", "discipline", "room", "lab_session", "registered_by")

_SESSION_RELATED = (
    "lab_work",
    "room",
    "room__laboratory",
    "room__training_center",
    "teacher",
    "semester",
)


def staff_bookings_qs(user):
    """Брони в зоне доступа сотрудника."""
    if user.role == UserRole.SYS_ADMIN:
        return Booking.objects.select_related(*_BOOKING_RELATED)
    lab = resolve_staff_laboratory(user)
    if not lab:
        return Booking.objects.none()
    return (
        Booking.objects.filter(
            Q(room__laboratory=lab)
            | Q(room__laboratory__isnull=True, room__training_center=lab.training_center)
            | Q(lab_work__laboratories=lab)
            | Q(discipline__laboratories=lab)
        )
        .distinct()
        .select_related(*_BOOKING_RELATED)
    )


def staff_can_access_booking(user, booking: Booking) -> bool:
    return staff_bookings_qs(user).filter(pk=booking.pk).exists()


def staff_support_tickets_qs(user):
    """Обращения: скоуп по учебному центру лаборатории сотрудника."""
    if user.role == UserRole.SYS_ADMIN:
        return SupportTicket.objects.select_related("student", "training_center")
    tc = resolve_staff_training_center(user)
    if not tc:
        return SupportTicket.objects.none()
    return SupportTicket.objects.filter(training_center=tc).select_related(
        "student", "training_center"
    )


def staff_sessions_qs(user, date_from=None, date_to=None):
    """Слоты в зоне доступа сотрудника с опционным фильтром по датам."""
    if user.role == UserRole.SYS_ADMIN:
        qs = LabSession.objects.all()
    else:
        lab = resolve_staff_laboratory(user)
        if not lab:
            return LabSession.objects.none()
        qs = LabSession.objects.filter(
            Q(room__laboratory=lab)
            | Q(room__laboratory__isnull=True, room__training_center=lab.training_center)
            | Q(lab_work__laboratories=lab)
        ).distinct()
    qs = qs.select_related(*_SESSION_RELATED)
    if date_from:
        qs = qs.filter(starts_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(starts_at__date__lte=date_to)
    return qs
