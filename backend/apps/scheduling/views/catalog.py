"""Управление каталогом лаборатории (день 7): аудитории и лабораторные работы.

Каскады: блокировка аудитории отменяет её будущие слоты (записи студентов →
SLOT_CANCELLED); изменение длительности ЛР синхронизирует открытые слоты
(apps/scheduling/services/session_admin.py).
"""

from datetime import datetime

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from apps.academics.models import ALLOWED_LAB_DURATIONS, LabWorkBookingRelease
from apps.bookings.services.booking import BookingService
from apps.scheduling.models import LabSession, LabSessionStatus
from apps.scheduling.services.catalog_admin import (
    CatalogError,
    create_lab_work,
    create_room,
    lab_work_booking_count,
    update_lab_work,
    update_room,
)
from apps.scheduling.services.session_admin import update_future_session_durations
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
            messages.success(request, f"Аудитория №{room.number} разблокирована.")
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
        lab_works = list(
            laboratory.lab_works.prefetch_related("disciplines", "disciplines__semester").order_by(
                "number", "title"
            )
        )
        edit_pk = request.GET.get("edit", "")
        edit_work = next((work for work in lab_works if str(work.pk) == edit_pk), None)
        disciplines = [
            binding.discipline
            for binding in laboratory.discipline_bindings.select_related("discipline").order_by(
                "discipline__title"
            )
        ]
        release_rows = self._release_rows(lab_works)
        return render(
            request,
            "scheduling/portal/lab_works.html",
            {
                "laboratory": laboratory,
                "lab_works": lab_works,
                "edit_work": edit_work,
                "form_disciplines": disciplines,
                "duration_choices": DURATION_CHOICES,
                "release_rows": release_rows,
            },
        )

    def _release_rows(self, lab_works):
        release_map = self._release_map(lab_works)
        rows = []
        for work in lab_works:
            for discipline in work.disciplines.all():
                release = release_map.get((work.pk, discipline.semester_id))
                opens_at = ""
                if release and release.booking_opens_at:
                    opens_at = release.booking_opens_at.strftime("%Y-%m-%dT%H:%M")
                rows.append(
                    {
                        "lab_work": work,
                        "discipline": discipline,
                        "release": release,
                        "opens_at": opens_at,
                    }
                )
        return rows

    def post(self, request):
        action = request.POST.get("action")
        if action == "release":
            return self._update_release(request)
        messages.error(request, "Неизвестное действие.")
        return redirect("lab-head-lab-works")

    def _release_map(self, lab_works):
        work_ids = [work.pk for work in lab_works]
        rows = LabWorkBookingRelease.objects.filter(lab_work_id__in=work_ids)
        return {(row.lab_work_id, row.semester_id): row for row in rows}

    def _update_release(self, request):
        lab_work = self.laboratory.lab_works.filter(pk=request.POST.get("lab_work")).first()
        semester_id = request.POST.get("semester")
        raw_opens_at = request.POST.get("booking_opens_at", "").strip()
        if lab_work is None or not semester_id:
            messages.error(request, "Некорректные параметры записи.")
            return redirect("lab-head-lab-works")
        if raw_opens_at:
            try:
                naive = datetime.strptime(raw_opens_at, "%Y-%m-%dT%H:%M")
                opens_at = timezone.make_aware(naive)
            except (ValueError, TypeError):
                messages.error(request, "Некорректная дата открытия записи.")
                return redirect("lab-head-lab-works")
            LabWorkBookingRelease.objects.update_or_create(
                lab_work=lab_work,
                semester_id=int(semester_id),
                defaults={"booking_opens_at": opens_at},
            )
            messages.success(request, f"Запись на «{lab_work.title}» откроется {opens_at:%d.%m.%Y %H:%M}.")
        else:
            LabWorkBookingRelease.objects.filter(
                lab_work=lab_work, semester_id=int(semester_id)
            ).delete()
            messages.success(request, f"Дата открытия записи на «{lab_work.title}» сброшена.")
        return redirect("lab-head-lab-works")


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
        old_duration = lab_work.duration_minutes
        try:
            lab_work = update_lab_work(laboratory=self.laboratory, lab_work=lab_work, data=request.POST)
        except CatalogError as exc:
            messages.error(request, str(exc))
            return redirect("lab-head-lab-works")
        if lab_work.duration_minutes != old_duration:
            updated = update_future_session_durations(lab_work)
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
