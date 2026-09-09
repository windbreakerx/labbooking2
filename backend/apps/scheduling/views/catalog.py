"""Управление каталогом лаборатории (день 7): аудитории и лабораторные работы.

Каскады: блокировка аудитории отменяет её будущие слоты (записи студентов →
SLOT_CANCELLED), разблокировка — регенерирует; изменение мест/длительности ЛР
синхронизирует открытые слоты (bookings/services/schedule_sync.py).
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from apps.academics.models import ALLOWED_LAB_DURATIONS, Semester
from apps.bookings.services.booking import BookingService
from apps.bookings.services.schedule_sync import (
    sync_open_session_capacities,
    sync_open_session_durations,
)
from apps.scheduling.models import LabSession, LabSessionStatus
from apps.scheduling.services.catalog_admin import (
    CatalogError,
    create_lab_work,
    create_room,
    lab_work_booking_count,
    update_lab_work,
    update_room,
)
from apps.scheduling.services.slot_generation import generate_lab_sessions
from apps.scheduling.views.portal import PortalLaboratoryMixin

DURATION_CHOICES = tuple((value, f"{value} мин") for value in ALLOWED_LAB_DURATIONS)


class LabHeadRoomsView(PortalLaboratoryMixin, View):
    def get(self, request):
        rooms = list(self.laboratory.rooms.order_by("number"))
        edit_pk = request.GET.get("edit", "")
        edit_room = next((room for room in rooms if str(room.pk) == edit_pk), None)
        return render(
            request,
            "scheduling/portal/rooms.html",
            {"laboratory": self.laboratory, "rooms": rooms, "edit_room": edit_room},
        )


class LabHeadRoomCreateView(PortalLaboratoryMixin, View):
    def post(self, request):
        try:
            room = create_room(laboratory=self.laboratory, data=request.POST)
        except CatalogError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Аудитория №{room.number} создана.")
        return redirect("lab-head-rooms")


class LabHeadRoomUpdateView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        room = self.laboratory.rooms.filter(pk=pk).first()
        if room is None:
            messages.error(request, "Аудитория не найдена в вашей лаборатории.")
            return redirect("lab-head-rooms")
        was_blocked = room.is_blocked
        try:
            room = update_room(laboratory=self.laboratory, room=room, data=request.POST)
        except CatalogError as exc:
            messages.error(request, str(exc))
            return redirect("lab-head-rooms")
        if room.is_blocked and not was_blocked:
            cancelled = _block_room_sessions(request, room)
            messages.success(
                request,
                f"Аудитория №{room.number} заблокирована, отменено будущих слотов: {cancelled}.",
            )
        elif was_blocked and not room.is_blocked:
            semester = Semester.objects.filter(is_active=True).first()
            if semester is not None:
                generate_lab_sessions(semester=semester)
            messages.success(request, f"Аудитория №{room.number} разблокирована — слоты будут сгенерированы.")
        else:
            messages.success(request, f"Аудитория №{room.number} обновлена.")
        return redirect("lab-head-rooms")


def _block_room_sessions(request, room) -> int:
    service = BookingService(actor=request.user)
    sessions = LabSession.objects.filter(
        room=room,
        starts_at__gt=timezone.now(),
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
    ).order_by("starts_at")
    cancelled = 0
    for session in sessions:
        service.cancel_session_bookings(session, note="Аудитория заблокирована")
        cancelled += 1
    return cancelled


class LabHeadLabWorksView(PortalLaboratoryMixin, View):
    def get(self, request):
        laboratory = self.laboratory
        lab_works = list(laboratory.lab_works.prefetch_related("disciplines").order_by("number", "title"))
        edit_pk = request.GET.get("edit", "")
        edit_work = next((work for work in lab_works if str(work.pk) == edit_pk), None)
        disciplines = [
            binding.discipline
            for binding in laboratory.discipline_bindings.select_related("discipline").order_by(
                "discipline__title"
            )
        ]
        return render(
            request,
            "scheduling/portal/lab_works.html",
            {
                "laboratory": laboratory,
                "lab_works": lab_works,
                "edit_work": edit_work,
                "form_disciplines": disciplines,
                "duration_choices": DURATION_CHOICES,
            },
        )


class LabHeadLabWorkCreateView(PortalLaboratoryMixin, View):
    def post(self, request):
        try:
            work = create_lab_work(laboratory=self.laboratory, data=request.POST)
        except CatalogError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Работа «{work.title}» создана.")
        return redirect("lab-head-lab-works")


class LabHeadLabWorkUpdateView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        lab_work = self.laboratory.lab_works.filter(pk=pk).first()
        if lab_work is None:
            messages.error(request, "Работа не найдена в вашей лаборатории.")
            return redirect("lab-head-lab-works")
        old_capacity = lab_work.capacity
        old_duration = lab_work.duration_minutes
        try:
            lab_work = update_lab_work(laboratory=self.laboratory, lab_work=lab_work, data=request.POST)
        except CatalogError as exc:
            messages.error(request, str(exc))
            return redirect("lab-head-lab-works")
        if lab_work.capacity != old_capacity:
            updated = sync_open_session_capacities(lab_work)
            messages.success(
                request, f"Работа обновлена, вместимость синхронизирована со слотами: {updated}."
            )
        elif lab_work.duration_minutes != old_duration:
            updated = sync_open_session_durations(lab_work)
            messages.success(request, f"Работа обновлена, длительность синхронизирована со слотами: {updated}.")
        else:
            messages.success(request, f"Работа «{lab_work.title}» обновлена.")
        return redirect("lab-head-lab-works")


class LabHeadLabWorkDeleteView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        lab_work = self.laboratory.lab_works.filter(pk=pk).first()
        if lab_work is None:
            messages.error(request, "Работа не найдена в вашей лаборатории.")
            return redirect("lab-head-lab-works")
        bookings = lab_work_booking_count(lab_work)
        if bookings:
            messages.error(
                request,
                f"По работе есть записи студентов ({bookings}) — удаление запрещено.",
            )
            return redirect("lab-head-lab-works")
        lab_work.delete()
        messages.success(request, f"Работа «{lab_work.title}» удалена.")
        return redirect("lab-head-lab-works")
