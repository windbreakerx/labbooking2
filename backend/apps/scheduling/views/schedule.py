"""Редактор расписания (день 7): серверная сетка пары×дни по комнате + формы записи.

Оркестрация мутаций: после create/update/delete/copy — генерация (insert-only)
и sync старых/новых координат; осиротевшие слоты отменяются вместе с записями
студентов (SLOT_CANCELLED). Формы серверные (?new=день-время / ?edit=pk),
без клиентского JS.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from apps.academics.models import Semester
from apps.academics.scope import laboratory_people_qs
from apps.bookings.services.schedule_sync import sync_future_sessions_for_slot
from apps.scheduling.models import ScheduleDutyRole, ScheduleEntry, WeekParity
from apps.scheduling.services.entry_match import entry_lab_work_ids
from apps.scheduling.services.schedule_admin import (
    ScheduleError,
    build_schedule_grid,
    copy_schedule_weekday,
    create_schedule_entry,
    delete_schedule_entry,
    entries_prefetched,
    entry_future_booking_conflicts,
    update_schedule_entry,
)
from apps.scheduling.services.slot_generation import generate_lab_sessions
from apps.scheduling.services.slot_grid import UNIVERSITY_PAIR_SLOTS
from apps.scheduling.views.portal import PortalLaboratoryMixin

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAY_CHOICES = tuple(enumerate(WEEKDAY_LABELS))
PAIR_START_CHOICES = tuple(
    (start.strftime("%H:%M"), f"{number} пара ({start:%H:%M})")
    for number, start, _end in UNIVERSITY_PAIR_SLOTS
)
DURATION_CHOICES = tuple((value, f"{value} мин") for value in (30, 45, 60, 90))
SCHEDULE_URL = "lab-head-schedule"
_ENTRY_PREFETCH = ("discipline_selections__lab_works", "discipline_selections__discipline")


def _active_semester() -> Semester | None:
    return Semester.objects.filter(is_active=True).first()


def _back(room_pk: int | None) -> str:
    if room_pk:
        return f"{reverse(SCHEDULE_URL)}?room={room_pk}"
    return reverse(SCHEDULE_URL)


def _regenerate(entry: ScheduleEntry) -> None:
    lab_work_ids = sorted(entry_lab_work_ids(entry))
    if lab_work_ids:
        generate_lab_sessions(semester=entry.semester, lab_work_ids=lab_work_ids)


def _sync_at(room, semester, weekday: int) -> None:
    sync_future_sessions_for_slot(room=room, semester=semester, weekday=weekday)


def _scoped_entry(laboratory, pk: int) -> ScheduleEntry | None:
    return (
        ScheduleEntry.objects.filter(pk=pk, room__laboratory=laboratory)
        .prefetch_related(*_ENTRY_PREFETCH)
        .first()
    )


def _selected_room(request, rooms):
    raw = request.GET.get("room", "")
    if raw.isdigit():
        for room in rooms:
            if room.pk == int(raw):
                return room
    return rooms[0] if rooms else None


def _entry_form_context(request, laboratory, room, semester):
    """Контекст формы записи: ?edit=pk — редактирование, ?new=день-ЧЧ:ММ — создание."""
    if room is None or semester is None:
        return {}
    disciplines = list(
        laboratory.discipline_bindings.select_related("discipline").order_by("discipline__title")
    )
    lab_works = list(laboratory.lab_works.prefetch_related("disciplines").order_by("number", "title"))
    context = {
        "form_disciplines": [binding.discipline for binding in disciplines],
        "form_works_by_discipline": [
            (
                binding.discipline,
                [work for work in lab_works if binding.discipline in work.disciplines.all()],
            )
            for binding in disciplines
        ],
        "form_people": laboratory_people_qs(laboratory),
        "weekday_choices": WEEKDAY_CHOICES,
        "pair_start_choices": PAIR_START_CHOICES,
        "duration_choices": DURATION_CHOICES,
        "parity_choices": WeekParity.choices,
        "duty_choices": ScheduleDutyRole.choices,
        "form_discipline_ids": set(),
        "form_lab_work_ids": set(),
    }
    edit_pk = request.GET.get("edit", "")
    if edit_pk.isdigit():
        entry = _scoped_entry(laboratory, int(edit_pk))
        if entry is not None and entry.room_id == room.pk:
            selected_works = [
                str(work.pk)
                for selection in entry.discipline_selections.all()
                for work in selection.lab_works.all()
            ]
            context.update(
                form_entry=entry,
                form_action="lab-head-schedule-update",
                form_initial={
                    "weekday": entry.weekday,
                    "start_time": entry.start_time.strftime("%H:%M"),
                    "duration_minutes": entry.duration_minutes,
                    "week_parity": entry.week_parity,
                    "duty_role": entry.duty_role,
                    "duty_person": entry.duty_person_id or "",
                    "capacity": entry.capacity,
                    "load_group_label": entry.load_group_label,
                    "manual_title": entry.manual_title,
                },
                form_discipline_ids={str(s.discipline_id) for s in entry.discipline_selections.all()},
                form_lab_work_ids=set(selected_works),
                delete_conflicts=entry_future_booking_conflicts(entry),
            )
            return context
    new_slot = request.GET.get("new", "")
    weekday, _, start_time = new_slot.partition("-")
    if weekday.isdigit() and start_time in {value for value, _label in PAIR_START_CHOICES}:
        context.update(
            form_action="lab-head-schedule-create",
            form_initial={"weekday": int(weekday), "start_time": start_time},
        )
    return context


class LabHeadScheduleView(PortalLaboratoryMixin, View):
    def get(self, request):
        laboratory = self.laboratory
        semester = _active_semester()
        rooms = list(laboratory.rooms.order_by("number"))
        room = _selected_room(request, rooms)
        parity = request.GET.get("parity", WeekParity.BOTH)
        if parity not in WeekParity.values:
            parity = WeekParity.BOTH
        context = {
            "laboratory": laboratory,
            "semester": semester,
            "rooms": rooms,
            "room": room,
            "parity": parity,
            "parity_choices": WeekParity.choices,
            "weekday_labels": WEEKDAY_LABELS,
        }
        if semester is not None and room is not None:
            entries = list(entries_prefetched(room, semester))
            rows, unplaced = build_schedule_grid(entries, parity_filter=parity)
            context["grid_rows"] = rows
            context["unplaced"] = unplaced
            context.update(_entry_form_context(request, laboratory, room, semester))
        return render(request, "scheduling/portal/schedule.html", context)


class LabHeadScheduleCreateView(PortalLaboratoryMixin, View):
    def post(self, request):
        semester = _active_semester()
        if semester is None:
            messages.error(request, "Нет активного семестра — расписание недоступно.")
            return redirect(SCHEDULE_URL)
        room_pk = int(raw) if (raw := request.POST.get("room", "")).isdigit() else None
        try:
            entry = create_schedule_entry(
                laboratory=self.laboratory, semester=semester, data=request.POST
            )
        except ScheduleError as exc:
            messages.error(request, str(exc))
            return redirect(_back(room_pk))
        _regenerate(entry)
        _sync_at(entry.room, entry.semester, entry.weekday)
        messages.success(request, "Запись расписания создана — будущие слоты обновлены.")
        return redirect(_back(entry.room_id))


class LabHeadScheduleUpdateView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        entry = _scoped_entry(self.laboratory, pk)
        if entry is None:
            messages.error(request, "Запись расписания не найдена в вашей лаборатории.")
            return redirect(SCHEDULE_URL)
        old_weekday = entry.weekday
        try:
            entry = update_schedule_entry(laboratory=self.laboratory, entry=entry, data=request.POST)
        except ScheduleError as exc:
            messages.error(request, str(exc))
            return redirect(_back(entry.room_id))
        if old_weekday != entry.weekday:
            _sync_at(entry.room, entry.semester, old_weekday)
        _regenerate(entry)
        _sync_at(entry.room, entry.semester, entry.weekday)
        messages.success(request, "Запись расписания обновлена — будущие слоты синхронизированы.")
        return redirect(_back(entry.room_id))


class LabHeadScheduleDeleteView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        entry = _scoped_entry(self.laboratory, pk)
        if entry is None:
            messages.error(request, "Запись расписания не найдена в вашей лаборатории.")
            return redirect(SCHEDULE_URL)
        deleted, conflicts = delete_schedule_entry(entry, force=request.POST.get("confirm") == "yes")
        if not deleted:
            messages.error(
                request,
                f"На слот есть будущие записи студентов ({conflicts}) — подтвердите удаление.",
            )
            return redirect(f"{_back(entry.room_id)}&edit={entry.pk}")
        _sync_at(entry.room, entry.semester, entry.weekday)
        messages.success(request, "Запись удалена — осиротевшие слоты отменены.")
        return redirect(_back(entry.room_id))


class LabHeadScheduleCopyDayView(PortalLaboratoryMixin, View):
    def post(self, request):
        semester = _active_semester()
        if semester is None:
            messages.error(request, "Нет активного семестра — расписание недоступно.")
            return redirect(SCHEDULE_URL)
        room_pk = int(raw) if (raw := request.POST.get("room", "")).isdigit() else None
        room = self.laboratory.rooms.filter(pk=room_pk).first() if room_pk else None
        if room is None:
            messages.error(request, "Аудитория не найдена в вашей лаборатории.")
            return redirect(SCHEDULE_URL)
        source = request.POST.get("source_weekday", "")
        target = request.POST.get("target_weekday", "")
        try:
            created, skipped = copy_schedule_weekday(
                room=room,
                semester=semester,
                source_weekday=int(source) if source.isdigit() else -1,
                target_weekday=int(target) if target.isdigit() else -1,
                parity_filter=request.POST.get("week_parity") or WeekParity.BOTH,
            )
        except ScheduleError as exc:
            messages.error(request, str(exc))
            return redirect(_back(room.pk))
        if created:
            generate_lab_sessions(semester=semester)
            _sync_at(room, semester, int(target))
        messages.success(
            request, f"Скопировано записей: {created}, пропущено занятых слотов: {skipped}."
        )
        return redirect(_back(room.pk))
