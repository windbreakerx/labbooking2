"""XLSX-отчёты по записям и расписанию (день 7 / фаза 5).

Скоуп — staff_bookings_qs / staff_sessions_qs (зона сотрудника, SYS_ADMIN видит всё).
Плоские таблицы без стилизации. had_no_show — отдельная колонка/счётчик.
"""

from io import BytesIO

from django.db.models import Count, Q
from openpyxl import Workbook

from apps.academics.models import GroupDisciplineLoad, LabWork, StudentGroup
from apps.academics.scope import lab_head_disciplines_qs
from apps.bookings.models import Booking, BookingStatus
from apps.bookings.scope import staff_bookings_qs, staff_sessions_qs
from apps.scheduling.models import LabSession, LabSessionStatus
from apps.users.models import UserRole

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


# --- Загруженность аудиторий --------------------------------------------------


def room_load_rows(user, date_from=None, date_to=None):
    """Агрегаты по аудиториям: слоты, часы, записи, загрузка."""
    sessions = staff_sessions_qs(user, date_from=date_from, date_to=date_to)
    session_ids = list(sessions.values_list("pk", flat=True))
    bookings_counts = {
        row["lab_session_id"]: row["count"]
        for row in Booking.objects.filter(
            lab_session_id__in=session_ids,
            current_status__in=(BookingStatus.BOOKED, BookingStatus.VISITED),
        )
        .values("lab_session_id")
        .annotate(count=Count("pk"))
    }
    totals: dict[int, dict] = {}
    for session in sessions.order_by("room__training_center__number", "room__laboratory__name", "room__number"):
        room = session.room
        lab = room.laboratory
        tc = room.training_center
        key = room.pk
        entry = totals.get(key)
        if entry is None:
            entry = {
                "tc_number": tc.number if tc else "—",
                "tc_name": tc.name if tc else "",
                "lab_name": lab.name if lab else "—",
                "room_number": room.number,
                "room_name": room.name or "",
                "sessions": 0,
                "minutes": 0,
                "bookings": 0,
                "capacity": 0,
            }
            totals[key] = entry
        entry["sessions"] += 1
        entry["minutes"] += session.lab_work.duration_minutes
        entry["bookings"] += bookings_counts.get(session.pk, 0)
        entry["capacity"] += session.capacity
    rows = sorted(
        totals.values(),
        key=lambda r: (r["tc_number"], r["lab_name"], r["room_number"]),
    )
    for row in rows:
        row["hours"] = round(row["minutes"] / 60, 1)
        row["utilization"] = round(row["bookings"] / row["capacity"] * 100, 1) if row["capacity"] else 0
    return rows


def export_room_load_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Загруженность аудиторий"
    ws.append(["УЦ", "Лаборатория", "Аудитория", "Слотов", "Часов", "Записей", "Вместимость", "Загрузка %"])
    for row in rows:
        ws.append([
            row["tc_number"],
            row["lab_name"],
            f"{row['room_number']} {row['room_name']}".strip(),
            row["sessions"],
            row["hours"],
            row["bookings"],
            row["capacity"],
            row["utilization"],
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Загруженность преподавателей ---------------------------------------------


def teacher_load_rows(user, date_from=None, date_to=None, teacher_id=None):
    """Агрегаты по преподавателям. TEACHER видит только себя."""
    sessions = staff_sessions_qs(user, date_from=date_from, date_to=date_to)
    if teacher_id:
        sessions = sessions.filter(teacher_id=teacher_id)
    elif user.role == UserRole.TEACHER:
        sessions = sessions.filter(teacher=user)

    session_ids = list(sessions.values_list("pk", flat=True))
    bookings_counts = {
        row["lab_session_id"]: row["count"]
        for row in Booking.objects.filter(
            lab_session_id__in=session_ids,
            current_status__in=(BookingStatus.BOOKED, BookingStatus.VISITED),
        )
        .values("lab_session_id")
        .annotate(count=Count("pk"))
    }
    totals: dict[int, dict] = {}
    for session in sessions.order_by("teacher__last_name", "teacher__first_name"):
        teacher = session.teacher
        key = teacher.pk if teacher else 0
        entry = totals.get(key)
        if entry is None:
            entry = {
                "teacher": teacher,
                "teacher_name": teacher.get_full_name() if teacher else "Не назначен",
                "sessions": 0,
                "minutes": 0,
                "bookings": 0,
            }
            totals[key] = entry
        entry["sessions"] += 1
        entry["minutes"] += session.lab_work.duration_minutes
        entry["bookings"] += bookings_counts.get(session.pk, 0)
    rows = sorted(totals.values(), key=lambda r: r["teacher_name"])
    for row in rows:
        row["hours"] = round(row["minutes"] / 60, 1)
    return rows


def export_teacher_load_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Загруженность преподавателей"
    ws.append(["Преподаватель", "Слотов", "Часов", "Записей"])
    for row in rows:
        ws.append([row["teacher_name"], row["sessions"], row["hours"], row["bookings"]])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Выполнение учебного плана ------------------------------------------------


def curriculum_rows(user, semester_id=None):
    """Прогресс групп по ЛР с учётом required_sessions.

    Для каждой дисциплины в скоупе, каждой группы из нагрузки и каждой ЛР
    считаем: требуется посещений = required_sessions * студентов в группе.
    """
    disciplines = lab_head_disciplines_qs(user)
    if semester_id:
        disciplines = disciplines.filter(semester_id=semester_id)
    discipline_ids = list(disciplines.values_list("pk", flat=True))
    if not discipline_ids:
        return []

    load_qs = GroupDisciplineLoad.objects.filter(
        discipline_id__in=discipline_ids,
        is_active=True,
        semester__is_active=True,
    ).select_related("group", "discipline", "semester")
    if semester_id:
        load_qs = load_qs.filter(semester_id=semester_id)

    group_ids = list(load_qs.values_list("group_id", flat=True).distinct())
    group_sizes = {
        row["pk"]: row["count"]
        for row in StudentGroup.objects.filter(pk__in=group_ids)
        .annotate(count=Count("students"))
        .values("pk", "count")
    }

    lab_work_ids = list(
        LabWork.objects.filter(
            is_published=True, disciplines__id__in=discipline_ids
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    visited_counts = {
        (row["lab_work_id"], row["group_id"]): row["count"]
        for row in Booking.objects.filter(
            lab_work_id__in=lab_work_ids,
            current_status=BookingStatus.VISITED,
            student__profile__student_group_id__in=group_ids,
        )
        .values("lab_work_id", "student__profile__student_group_id")
        .annotate(count=Count("pk"))
    }

    rows = []
    for load in load_qs.order_by("discipline__title", "group__name"):
        group_size = group_sizes.get(load.group_id, 0)
        if group_size == 0:
            continue
        for lab_work in LabWork.objects.filter(
            is_published=True, disciplines=load.discipline_id
        ).order_by("number", "title"):
            required_total = lab_work.required_sessions * group_size
            visited = visited_counts.get((lab_work.pk, load.group_id), 0)
            rows.append({
                "discipline": load.discipline,
                "semester": load.semester,
                "group": load.group,
                "lab_work": lab_work,
                "students": group_size,
                "required_sessions": lab_work.required_sessions,
                "required_total": required_total,
                "visited": visited,
                "progress": round(visited / required_total * 100, 1) if required_total else 0,
            })
    return rows


def export_curriculum_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Выполнение учебного плана"
    ws.append([
        "Семестр", "Дисциплина", "Группа", "Лабораторная работа",
        "Студентов", "Требуется посещений", "Посещено", "Прогресс %",
    ])
    for row in rows:
        ws.append([
            str(row["semester"]),
            row["discipline"].title,
            row["group"].name,
            row["lab_work"].title,
            row["students"],
            row["required_total"],
            row["visited"],
            row["progress"],
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Dashboard KPI ------------------------------------------------------------


def dashboard_kpi(laboratory=None):
    """Ключевые метрики для dashboard завлаба.

    Если laboratory передана — метрики по лаборатории, иначе по всем.
    """
    from django.utils import timezone

    now = timezone.now()
    session_qs = LabSession.objects.all()
    booking_qs = Booking.objects.all()
    if laboratory is not None:
        session_qs = session_qs.filter(
            Q(room__laboratory=laboratory) | Q(lab_work__laboratories=laboratory)
        ).distinct()
        booking_qs = booking_qs.filter(
            Q(room__laboratory=laboratory)
            | Q(room__laboratory__isnull=True, room__training_center=laboratory.training_center)
            | Q(lab_work__laboratories=laboratory)
            | Q(discipline__laboratories=laboratory)
        ).distinct()

    total_sessions = session_qs.count()
    upcoming_sessions = session_qs.filter(starts_at__gt=now).count()
    status_counts = dict(
        booking_qs.values("current_status").annotate(count=Count("pk")).values_list("current_status", "count")
    )
    visited = status_counts.get(BookingStatus.VISITED, 0)
    booked = status_counts.get(BookingStatus.BOOKED, 0)
    no_show = status_counts.get(BookingStatus.NO_SHOW, 0)
    cancelled = (
        status_counts.get(BookingStatus.CANCELLED, 0)
        + status_counts.get(BookingStatus.SLOT_CANCELLED, 0)
    )
    return {
        "total_sessions": total_sessions,
        "upcoming_sessions": upcoming_sessions,
        "visited": visited,
        "booked": booked,
        "no_show": no_show,
        "cancelled": cancelled,
    }


def dashboard_no_show_top(laboratory=None, limit: int = 10):
    """Студенты с наибольшим числом неявок в зоне лаборатории."""
    qs = Booking.objects.filter(current_status=BookingStatus.NO_SHOW)
    if laboratory is not None:
        qs = qs.filter(
            Q(room__laboratory=laboratory)
            | Q(lab_work__laboratories=laboratory)
            | Q(discipline__laboratories=laboratory)
        ).distinct()
    return list(
        qs.values("student__pk", "student__last_name", "student__first_name")
        .annotate(count=Count("pk"))
        .order_by("-count")[:limit]
    )


def dashboard_room_utilization(laboratory=None):
    """Загрузка аудиторий (%) для dashboard."""
    sessions = LabSession.objects.exclude(status=LabSessionStatus.CANCELLED)
    if laboratory is not None:
        sessions = sessions.filter(room__laboratory=laboratory)
    session_ids = list(sessions.values_list("pk", flat=True))
    bookings_counts = {
        row["lab_session_id"]: row["count"]
        for row in Booking.objects.filter(
            lab_session_id__in=session_ids,
            current_status__in=(BookingStatus.BOOKED, BookingStatus.VISITED),
        )
        .values("lab_session_id")
        .annotate(count=Count("pk"))
    }
    rows = []
    for session in sessions.select_related("room"):
        rows.append({
            "room": f"{session.room.number} {session.room.name or ''}".strip(),
            "booked": bookings_counts.get(session.pk, 0),
            "capacity": session.capacity,
        })
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(row["room"], {"booked": 0, "capacity": 0})
        entry["booked"] += row["booked"]
        entry["capacity"] += row["capacity"]
    return [
        {"room": room, "utilization": round(e["booked"] / e["capacity"] * 100, 1) if e["capacity"] else 0}
        for room, e in sorted(totals.items())
    ]


def dashboard_curriculum_summary(laboratory=None):
    """Сводное выполнение плана по дисциплинам для dashboard."""
    from apps.academics.models import Discipline

    qs = Discipline.objects.select_related("semester").filter(semester__is_active=True)
    if laboratory is not None:
        qs = qs.filter(laboratories=laboratory)
    result = []
    for discipline in qs.order_by("title"):
        rows = curriculum_rows_for_discipline(discipline)
        if not rows:
            continue
        required = sum(r["required_total"] for r in rows)
        visited = sum(r["visited"] for r in rows)
        result.append({
            "discipline": discipline.title,
            "semester": str(discipline.semester),
            "progress": round(visited / required * 100, 1) if required else 0,
        })
    return result


def curriculum_rows_for_discipline(discipline):
    """Хелпер: строки curriculum для одной дисциплины (любой viewer)."""
    rows = []
    lab_works = list(
        LabWork.objects.filter(is_published=True, disciplines=discipline).order_by("number", "title")
    )
    if not lab_works:
        return rows
    loads = GroupDisciplineLoad.objects.filter(
        discipline=discipline, is_active=True, semester__is_active=True
    ).select_related("group")
    group_ids = [load.group_id for load in loads]
    group_sizes = {
        row["pk"]: row["count"]
        for row in StudentGroup.objects.filter(pk__in=group_ids)
        .annotate(count=Count("students"))
        .values("pk", "count")
    }
    visited_counts = {
        (row["lab_work_id"], row["group_id"]): row["count"]
        for row in Booking.objects.filter(
            lab_work__in=lab_works,
            current_status=BookingStatus.VISITED,
            student__profile__student_group_id__in=group_ids,
        )
        .values("lab_work_id", "student__profile__student_group_id")
        .annotate(count=Count("pk"))
    }
    for load in loads.order_by("group__name"):
        group_size = group_sizes.get(load.group_id, 0)
        if group_size == 0:
            continue
        for lab_work in lab_works:
            required_total = lab_work.required_sessions * group_size
            visited = visited_counts.get((lab_work.pk, load.group_id), 0)
            rows.append({
                "group": load.group,
                "lab_work": lab_work,
                "required_total": required_total,
                "visited": visited,
            })
    return rows
