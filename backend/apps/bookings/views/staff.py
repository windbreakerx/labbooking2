"""Стафф-UI (день 7): таблица броней, смена статуса, ручная запись.

Правила — в сервисах: скоуп — staff_bookings_qs, фильтры/сортировка —
staff_filters, слоты ручной записи — staff_manual_slots, мутации —
BookingService. TEACHER — только чтение (staff_can_modify_bookings).
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from apps.academics.models import Semester
from apps.academics.scope import (
    staff_disciplines_qs,
    staff_manual_lab_work_items,
    staff_students_qs,
    staff_writable_lab_works_qs,
)
from apps.bookings.models import Booking, BookingStatus
from apps.bookings.params import parse_id
from apps.bookings.reports import XLSX_CONTENT_TYPE, generate_report
from apps.bookings.scope import staff_bookings_qs, staff_can_modify_room, staff_rooms_qs
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.booking_mutations import STATUS_TRANSITIONS
from apps.bookings.services.session_catalog import staff_manual_slots
from apps.bookings.services.staff_filters import (
    BOOKING_SORT_FIELDS,
    filter_staff_bookings,
    order_bookings_queryset,
    search_students_for_staff,
)
from apps.bookings.views.student import _client_ip
from apps.scheduling.models import LabSession
from apps.users.mixins import StaffRequiredMixin
from apps.users.models import User, UserRole
from apps.users.roles import staff_can_modify_bookings

STATUS_LABELS = {
    BookingStatus.VISITED: "Посетил",
    BookingStatus.NO_SHOW: "Неявка",
    BookingStatus.REACCESS: "Повторный доступ",
    BookingStatus.CANCELLED: "Отмена записи",
    BookingStatus.SLOT_CANCELLED: "Слот отменён",
}
TABLE_COLUMNS = [
    ("student", "Студент"),
    ("group", "Группа"),
    ("discipline", "Дисциплина"),
    ("lab_work", "ЛР"),
    ("date", "Дата"),
    ("room", "Ауд."),
    ("registration", "Регистрация"),
    ("status", "Статус"),
]


def _allowed_statuses(booking: Booking) -> list[dict]:
    options = sorted(STATUS_TRANSITIONS.get(booking.current_status, set()))
    if booking.current_status == BookingStatus.BOOKED:
        options.append(BookingStatus.CANCELLED)
    return [{"status": status, "label": STATUS_LABELS[status]} for status in options]


def _header_links(request) -> list[dict]:
    """Ссылки сортировки: текущая колонка переключает направление, фильтры сохраняются."""
    links = []
    for key, label in TABLE_COLUMNS:
        params = request.GET.copy()
        params["sort"] = key
        default_dir = BOOKING_SORT_FIELDS[key][1]
        if request.GET.get("sort") == key:
            params["dir"] = "desc" if request.GET.get("dir", default_dir) == "asc" else "asc"
        else:
            params["dir"] = default_dir
        links.append({"key": key, "label": label, "href": "?" + params.urlencode()})
    return links


def _require_modify(request):
    """Ручная запись/статусы — только LAB_ADMIN/LAB_HEAD/SYS_ADMIN (HTMX → 403)."""
    if staff_can_modify_bookings(request.user):
        return None
    if request.headers.get("HX-Request") == "true":
        return HttpResponseForbidden()
    messages.error(request, "Недостаточно прав для изменения записей.")
    return redirect("staff-bookings")


class StaffBookingsView(StaffRequiredMixin, View):
    def get(self, request):
        qs = staff_bookings_qs(request.user).select_related(
            "student__profile", "student__profile__student_group"
        )
        bookings = list(
            order_bookings_queryset(filter_staff_bookings(qs, request.GET), request.GET)
        )
        paginator = Paginator(bookings, 50)
        page_obj = paginator.get_page(request.GET.get("page"))
        can_modify = staff_can_modify_bookings(request.user)
        writable_room_ids = (
            set(staff_rooms_qs(request.user).values_list("pk", flat=True))
            if can_modify
            else set()
        )
        can_write = bool(writable_room_ids)
        rows = [
            {
                "booking": booking,
                "transitions": _allowed_statuses(booking),
                "can_modify": booking.room_id in writable_room_ids,
            }
            for booking in page_obj.object_list
        ]
        pagination_params = request.GET.copy()
        pagination_params.pop("page", None)
        pagination_query = f"{pagination_params.urlencode()}&" if pagination_params else ""
        return render(
            request,
            "bookings/staff/bookings.html",
            {
                "rows": rows,
                "columns": _header_links(request),
                "filters": request.GET,
                "disciplines": staff_disciplines_qs(request.user),
                "status_choices": Booking._meta.get_field("current_status").choices,
                "can_modify": can_write,
                "show_no_rooms_hint": can_modify and not can_write,
                "page_obj": page_obj,
                "pagination_query": pagination_query,
            },
        )


class StaffBookingStatusView(StaffRequiredMixin, View):
    def post(self, request, pk):
        denied = _require_modify(request)
        if denied:
            return denied
        booking = staff_bookings_qs(request.user).filter(pk=pk).first()
        if booking is None:
            messages.error(request, "Запись недоступна в вашей зоне доступа.")
            return redirect("staff-bookings")
        if not staff_can_modify_room(request.user, booking.room):
            messages.error(
                request,
                "Нет прав на изменение записей в этой аудитории. Обратитесь к завлабу.",
            )
            return redirect("staff-bookings")
        new_status = request.POST.get("status", "")
        service = BookingService(actor=request.user, ip_address=_client_ip(request))
        try:
            service.change_status(booking, new_status, note=request.POST.get("note", ""))
        except BookingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request, f"Статус изменён на «{STATUS_LABELS.get(new_status, new_status)}»."
            )
        return redirect("staff-bookings")


class StaffManualBookingView(StaffRequiredMixin, View):
    def get(self, request):
        denied = _require_modify(request)
        if denied:
            return denied
        return render(request, "bookings/staff/manual_booking.html")

    def post(self, request):
        denied = _require_modify(request)
        if denied:
            return denied
        student = User.objects.filter(
            pk=request.POST.get("student_id") or 0, role=UserRole.STUDENT
        ).first()
        if student is None:
            messages.error(request, "Выберите студента из результатов поиска.")
            return redirect("staff-manual-booking")
        session = LabSession.objects.filter(pk=request.POST.get("session_id") or 0).first()
        if session is None:
            messages.error(request, "Выберите слот для записи.")
            return redirect("staff-manual-booking")
        if not staff_writable_lab_works_qs(request.user).filter(pk=session.lab_work_id).exists():
            messages.error(request, "Лабораторная работа недоступна для вашей лаборатории.")
            return redirect("staff-manual-booking")
        if not staff_can_modify_room(request.user, session.room):
            messages.error(
                request,
                "Нет прав на запись в эту аудиторию. Обратитесь к завлабу.",
            )
            return redirect("staff-manual-booking")
        service = BookingService(actor=request.user, ip_address=_client_ip(request))
        try:
            service.create_booking(student, session.pk, manual=True)
        except BookingError as exc:
            messages.error(request, str(exc))
            return redirect("staff-manual-booking")
        messages.success(request, f"Студент {student.first_name} {student.last_name} записан вручную.")
        for warning in service.booking_warnings:
            messages.warning(request, warning)
        return redirect("staff-bookings")


class StaffManualStudentSearchView(StaffRequiredMixin, View):
    """HTMX-фрагмент: поиск студента (ФИО/email/группа, минимум 2 символа)."""

    def get(self, request):
        denied = _require_modify(request)
        if denied:
            return denied
        query = request.GET.get("q", "").strip()
        return render(
            request,
            "bookings/staff/_student_results.html",
            {"students": list(search_students_for_staff(query)), "query": query},
        )


class StaffManualLabWorksView(StaffRequiredMixin, View):
    """HTMX-фрагмент: ЛР из плана группы студента ∩ зона сотрудника."""

    def get(self, request):
        denied = _require_modify(request)
        if denied:
            return denied
        student_id = request.GET.get("student_id", "")
        student = User.objects.filter(
            pk=student_id if student_id.isdigit() else 0, role=UserRole.STUDENT
        ).first()
        if student is None or not staff_students_qs(request.user).filter(pk=student.pk).exists():
            return HttpResponseForbidden()
        lab_works = list(staff_manual_lab_work_items(request.user, student))
        return render(
            request,
            "bookings/staff/_lab_work_options.html",
            {"lab_works": lab_works, "student": student},
        )


class StaffManualSlotsView(StaffRequiredMixin, View):
    """HTMX-фрагмент: слоты ЛР в окне ручной записи, сгруппированы по датам."""

    def get(self, request):
        denied = _require_modify(request)
        if denied:
            return denied
        lab_work_id = request.GET.get("lab_work_id", "")
        lab_work = staff_writable_lab_works_qs(request.user).filter(
            pk=lab_work_id if lab_work_id.isdigit() else 0
        ).first()
        if lab_work is None:
            return HttpResponseForbidden()
        by_date: dict = {}
        room_ids = staff_rooms_qs(request.user).values_list("pk", flat=True)
        for slot in staff_manual_slots(lab_work.pk, room_ids=room_ids):
            by_date.setdefault(timezone.localtime(slot.session.starts_at).date(), []).append(slot)
        return render(
            request,
            "bookings/staff/_manual_slots.html",
            {"lab_work": lab_work, "by_date": sorted(by_date.items())},
        )


class StaffReportsView(StaffRequiredMixin, View):
    """Лендинг отчётов: период/дисциплина применяются к ссылкам выгрузок."""

    def get(self, request):
        semester_id = parse_id(request.GET.get("semester"))
        active_semester = Semester.objects.filter(is_active=True).first()
        selected_semester_id = semester_id or (active_semester.pk if active_semester else None)
        query = {
            key: request.GET[key]
            for key in ("date_from", "date_to", "discipline")
            if request.GET.get(key)
        }
        if selected_semester_id:
            query["semester"] = selected_semester_id
        return render(
            request,
            "bookings/staff/reports.html",
            {
                "filters": request.GET,
                "query_string": urlencode(query),
                "disciplines": staff_disciplines_qs(request.user),
                "semesters": Semester.objects.order_by("-start_date"),
                "selected_semester_id": selected_semester_id,
            },
        )


class StaffReportDownloadView(StaffRequiredMixin, View):
    def get(self, request, report_type):
        payload = generate_report(
            report_type,
            date_from=request.GET.get("date_from", ""),
            date_to=request.GET.get("date_to", ""),
            discipline_id=request.GET.get("discipline", ""),
            staff_user=request.user,
            semester_id=parse_id(request.GET.get("semester")),
        )
        response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{report_type}.xlsx"'
        return response
