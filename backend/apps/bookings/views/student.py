"""Студентческий UI (день 6): каталог дисциплин, wizard записи, мои записи.

Правила живут в сервисах: видимость каталога — academics.scope, слоты —
student_bookable_slots (окно/праздники/места/whitelist/занятость), создание
и отмена — BookingService. Views собирают контекст и выводят ошибки сервисов.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.academics.models import Discipline, LabWork
from apps.academics.scope import (
    resolve_student_group,
    student_can_access_discipline,
    student_can_access_lab_work,
    student_disciplines_qs,
    student_lab_works_qs,
)
from apps.bookings.models import Booking, BookingStatus
from apps.bookings.services import BookingError, BookingService
from apps.bookings.services.session_catalog import student_bookable_slots
from apps.users.mixins import StudentRequiredMixin


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _lab_work_status_map(user, lab_works) -> dict[int, str]:
    """Статус записи студента по ЛР: BOOKED → «вы записаны», VISITED → «пройдена»."""
    rows = (
        user.bookings.filter(
            lab_work_id__in=[lab_work.pk for lab_work in lab_works],
            current_status__in=[BookingStatus.BOOKED, BookingStatus.VISITED],
        )
        .order_by("-scheduled_at")
        .values_list("lab_work_id", "current_status")
    )
    statuses = {}
    for lab_work_id, status in rows:
        statuses.setdefault(lab_work_id, status)
    return statuses


class DisciplinesView(StudentRequiredMixin, View):
    def get(self, request):
        group = resolve_student_group(request.user)
        counts: dict[int, int] = {}
        for lab_work in student_lab_works_qs(request.user).prefetch_related("disciplines"):
            for discipline in lab_work.disciplines.all():
                counts[discipline.pk] = counts.get(discipline.pk, 0) + 1
        rows = [
            {"discipline": discipline, "lab_work_count": counts.get(discipline.pk, 0)}
            for discipline in student_disciplines_qs(request.user)
            .select_related("department__faculty")
            .order_by("title")
        ]
        return render(
            request, "bookings/student/disciplines.html", {"rows": rows, "group": group}
        )


class LabWorksView(StudentRequiredMixin, View):
    def get(self, request, pk):
        if not student_can_access_discipline(request.user, pk):
            messages.error(request, "Дисциплина недоступна для вашей группы.")
            return redirect("disciplines")
        discipline = get_object_or_404(Discipline, pk=pk)
        lab_works = list(student_lab_works_qs(request.user, pk))
        statuses = _lab_work_status_map(request.user, lab_works)
        rows = [
            {
                "lab_work": lab_work,
                "status": statuses.get(lab_work.pk),
            }
            for lab_work in lab_works
        ]
        return render(
            request,
            "bookings/student/lab_works.html",
            {"discipline": discipline, "rows": rows},
        )


class BookWizardView(StudentRequiredMixin, View):
    """Слоты ЛР в окне записи, сгруппированные по датам (шаг 1 wizard'а)."""

    def get(self, request, pk):
        if not student_can_access_lab_work(request.user, pk):
            messages.error(request, "Лабораторная работа недоступна для вашей группы.")
            return redirect("disciplines")
        lab_work = get_object_or_404(LabWork, pk=pk, is_published=True)
        by_date: dict = {}
        for slot in student_bookable_slots(lab_work.pk, student=request.user):
            local_date = timezone.localtime(slot.session.starts_at).date()
            by_date.setdefault(local_date, []).append(slot)
        return render(
            request,
            "bookings/student/book.html",
            {"lab_work": lab_work, "by_date": sorted(by_date.items())},
        )


class BookConfirmView(StudentRequiredMixin, View):
    """Шаг 2: сводка слота → POST через BookingService (правила проверяет сервис)."""

    def _slot(self, request, lab_work, session_id):
        for slot in student_bookable_slots(lab_work.pk, student=request.user):
            if slot.session.pk == session_id:
                return slot
        return None

    def _guards(self, request, pk, session_id):
        if not student_can_access_lab_work(request.user, pk):
            messages.error(request, "Лабораторная работа недоступна для вашей группы.")
            return None, redirect("disciplines")
        lab_work = get_object_or_404(LabWork, pk=pk, is_published=True)
        slot = self._slot(request, lab_work, session_id)
        if slot is None:
            messages.error(request, "Слот недоступен для записи — выберите другой.")
            return None, redirect("book-lab-work", lab_work.pk)
        return (lab_work, slot), None

    def get(self, request, pk, session_id):
        found, redirect_response = self._guards(request, pk, session_id)
        if redirect_response is not None:
            return redirect_response
        lab_work, slot = found
        return render(
            request,
            "bookings/student/book_confirm.html",
            {"lab_work": lab_work, "slot": slot, "session": slot.session},
        )

    def post(self, request, pk, session_id):
        found, redirect_response = self._guards(request, pk, session_id)
        if redirect_response is not None:
            return redirect_response
        lab_work, _slot = found
        service = BookingService(actor=request.user, ip_address=_client_ip(request))
        try:
            booking = service.create_booking(student=request.user, session_id=session_id)
        except BookingError as exc:
            messages.error(request, str(exc))
            return redirect("book-lab-work", lab_work.pk)
        for warning in service.booking_warnings:
            messages.warning(request, warning)
        local_start = timezone.localtime(booking.scheduled_at)
        messages.success(
            request,
            f"Вы записаны: {booking.lab_work.title}, {local_start:%d.%m %H:%M}, {booking.room}.",
        )
        return redirect("my-bookings")


class MyBookingsView(StudentRequiredMixin, View):
    def get(self, request):
        bookings = list(
            request.user.bookings.select_related(
                "lab_work", "discipline", "room", "room__training_center"
            ).order_by("-scheduled_at")
        )
        now = timezone.now()
        upcoming = [
            booking
            for booking in bookings
            if booking.current_status == BookingStatus.BOOKED and booking.scheduled_at >= now
        ]
        history = [booking for booking in bookings if booking not in upcoming]
        return render(
            request,
            "bookings/student/my_bookings.html",
            {"upcoming": upcoming, "history": history},
        )


class CancelBookingView(StudentRequiredMixin, View):
    def post(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk, student=request.user)
        service = BookingService(actor=request.user, ip_address=_client_ip(request))
        try:
            service.cancel_booking(booking)
        except BookingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Запись отменена.")
        return redirect("my-bookings")
