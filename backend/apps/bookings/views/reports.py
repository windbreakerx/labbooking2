"""Views отчётов фазы 5: загруженность аудиторий/преподавателей, учебный план.

Права:
- завлаб/staff (LAB_ADMIN/LAB_HEAD/SYS_ADMIN) — все отчёты в своей зоне;
- преподаватель — только загруженность преподавателей, причём только своя;
- сисадмин — всё.
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from apps.academics.models import Semester
from apps.bookings.reports import (
    XLSX_CONTENT_TYPE,
    curriculum_rows,
    export_curriculum_xlsx,
    export_room_load_xlsx,
    export_teacher_load_xlsx,
    room_load_rows,
    teacher_load_rows,
)
from apps.users.mixins import StaffRequiredMixin
from apps.users.models import UserRole


def _parse_dates(request):
    return request.GET.get("date_from", ""), request.GET.get("date_to", "")


def _deny_if_teacher(request):
    """TEACHER не имеет доступа к отчётам по аудиториям/плану."""
    if request.user.role == UserRole.TEACHER:
        messages.error(request, "Недостаточно прав.")
        return redirect("home")
    return None


class RoomLoadReportView(StaffRequiredMixin, View):
    def get(self, request):
        denied = _deny_if_teacher(request)
        if denied:
            return denied
        date_from, date_to = _parse_dates(request)
        rows = room_load_rows(request.user, date_from=date_from or None, date_to=date_to or None)
        if request.GET.get("format") == "xlsx":
            payload = export_room_load_xlsx(rows)
            response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
            response["Content-Disposition"] = 'attachment; filename="room-load.xlsx"'
            return response
        return render(
            request,
            "bookings/staff/report_room_load.html",
            {"rows": rows, "filters": request.GET},
        )


class TeacherLoadReportView(StaffRequiredMixin, View):
    def get(self, request):
        date_from, date_to = _parse_dates(request)
        teacher_id = request.GET.get("teacher")
        # Преподаватель видит только себя, независимо от query string
        if request.user.role == UserRole.TEACHER:
            teacher_id = request.user.pk
        rows = teacher_load_rows(
            request.user,
            date_from=date_from or None,
            date_to=date_to or None,
            teacher_id=int(teacher_id) if teacher_id and teacher_id.isdigit() else None,
        )
        if request.GET.get("format") == "xlsx":
            payload = export_teacher_load_xlsx(rows)
            response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
            response["Content-Disposition"] = 'attachment; filename="teacher-load.xlsx"'
            return response
        teachers_qs = []
        if request.user.role != UserRole.TEACHER:
            # Список преподавателей в зоне доступа — через people лаборатории
            from apps.academics.scope import laboratory_people_qs, resolve_staff_laboratory

            lab = resolve_staff_laboratory(request.user)
            if lab is not None:
                teachers_qs = laboratory_people_qs(lab).filter(role=UserRole.TEACHER)
        return render(
            request,
            "bookings/staff/report_teacher_load.html",
            {
                "rows": rows,
                "filters": request.GET,
                "teachers": teachers_qs,
            },
        )


class CurriculumReportView(StaffRequiredMixin, View):
    def get(self, request):
        denied = _deny_if_teacher(request)
        if denied:
            return denied
        semester_id = request.GET.get("semester")
        rows = curriculum_rows(
            request.user,
            semester_id=int(semester_id) if semester_id and semester_id.isdigit() else None,
        )
        if request.GET.get("format") == "xlsx":
            payload = export_curriculum_xlsx(rows)
            response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
            response["Content-Disposition"] = 'attachment; filename="curriculum.xlsx"'
            return response
        semesters = list(Semester.objects.order_by("-start_date"))
        return render(
            request,
            "bookings/staff/report_curriculum.html",
            {"rows": rows, "filters": request.GET, "semesters": semesters},
        )
