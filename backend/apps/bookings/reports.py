"""XLSX-отчёты по записям (день 7): записи, посещаемость, свод по дисциплине.

Скоуп — staff_bookings_qs (зона сотрудника, SYS_ADMIN видит всё). Формат
колонок портирован из v1 (apps/bookings/reports.py); плоские таблицы —
без стилизации. had_no_show — отдельная колонка/счётчик, а не статус.
"""

from io import BytesIO

from django.db.models import Q
from openpyxl import Workbook

from apps.bookings.models import Booking, BookingStatus
from apps.bookings.scope import staff_bookings_qs

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_REPORT_RELATED = (
    "student__profile",
    "student__profile__student_group",
    "room__training_center",
)


def _group_label(booking: Booking) -> str:
    profile = getattr(booking.student, "profile", None)
    if profile is None:
        return ""
    if profile.student_group_id:
        return profile.student_group.name or ""
    return profile.group_name


def generate_report(
    report_type: str,
    date_from: str | None = None,
    date_to: str | None = None,
    discipline_id: str | None = None,
    staff_user=None,
) -> bytes:
    """Выгрузить отчёт в XLSX: bookings / attendance / discipline_summary."""
    qs = Booking.objects.all()
    if staff_user is not None:
        qs = staff_bookings_qs(staff_user)
    qs = qs.select_related(*_REPORT_RELATED)
    if date_from:
        qs = qs.filter(scheduled_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(scheduled_at__date__lte=date_to)
    if discipline_id:
        qs = qs.filter(discipline_id=discipline_id)

    wb = Workbook()
    ws = wb.active
    if report_type == "bookings":
        ws.title = "Записи"
        ws.append([
            "Студент", "Email", "Группа", "Дисциплина", "ЛР", "Дата",
            "УЦ", "Ауд.", "Регистрация", "Кем записан", "Статус", "Кто отменил", "Была неявка",
        ])
        for b in qs.order_by("scheduled_at"):
            ws.append([
                b.student.full_name,
                b.student.email,
                _group_label(b),
                b.discipline.title,
                b.lab_work.title,
                b.scheduled_at.strftime("%d.%m.%Y %H:%M"),
                b.room.training_center.number,
                b.room.number,
                b.get_registration_type_display(),
                b.registered_by.full_name if b.registered_by else "",
                b.get_current_status_display(),
                b.get_cancel_source_display() if b.cancel_source else "",
                "Да" if b.had_no_show else "Нет",
            ])
    elif report_type == "attendance":
        ws.title = "Посещаемость"
        qs = qs.filter(Q(current_status=BookingStatus.VISITED) | Q(had_no_show=True))
        ws.append(["Студент", "Дисциплина", "ЛР", "Дата", "Статус", "Была неявка"])
        for b in qs.order_by("scheduled_at"):
            ws.append([
                b.student.full_name,
                b.discipline.title,
                b.lab_work.title,
                b.scheduled_at.strftime("%d.%m.%Y %H:%M"),
                b.get_current_status_display(),
                "Да" if b.had_no_show else "Нет",
            ])
    elif report_type == "discipline_summary":
        ws.title = "Свод по дисциплине"
        ws.append([
            "Дисциплина", "Записано", "Посетили", "Неявки", "Отмены",
            "Слот отменён", "Повторный доступ",
        ])
        for row in _summary_rows(qs):
            ws.append(row)
    else:
        ws.append(["Неизвестный тип отчёта"])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _summary_rows(qs) -> list[list]:
    """Агрегаты по дисциплине: «Записано» — только активные BOOKED, неявки — по had_no_show."""
    counters: dict[str, dict[str, int]] = {}
    for b in qs:
        row = counters.setdefault(
            b.discipline.title,
            {"booked": 0, "visited": 0, "no_show": 0, "cancelled": 0, "slot_cancelled": 0, "reaccess": 0},
        )
        if b.current_status == BookingStatus.BOOKED:
            row["booked"] += 1
        elif b.current_status == BookingStatus.VISITED:
            row["visited"] += 1
        elif b.current_status == BookingStatus.CANCELLED:
            row["cancelled"] += 1
        elif b.current_status == BookingStatus.SLOT_CANCELLED:
            row["slot_cancelled"] += 1
        elif b.current_status == BookingStatus.REACCESS:
            row["reaccess"] += 1
        if b.had_no_show:
            row["no_show"] += 1
    return [
        [title, row["booked"], row["visited"], row["no_show"], row["cancelled"], row["slot_cancelled"], row["reaccess"]]
        for title, row in sorted(counters.items())
    ]
