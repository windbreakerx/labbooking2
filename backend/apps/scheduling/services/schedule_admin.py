"""CRUD записей расписания для портала завлаба (день 7): валидация, сетка пар, копирование дня.

Сервис чистый относительно bookings: оркестрация «generate + sync» после
мутаций записи живёт во view (apps/scheduling/views/schedule.py), чтобы
не заводить обратную зависимость scheduling → bookings. Отмена осиротевших
слотов — bookings/services/schedule_sync.py, генерация — slot_generation.py.
"""

from datetime import time

from apps.academics.models import ALLOWED_LAB_DURATIONS, Discipline, LabWork, Semester
from apps.academics.scope import laboratory_people_qs
from apps.scheduling.models import (
    Laboratory,
    Room,
    ScheduleDutyRole,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
    WeekParity,
)
from apps.scheduling.services.entry_match import (
    entry_matches_session,
    matching_entries_for_session,
)
from apps.scheduling.services.slot_grid import (
    UNIVERSITY_PAIR_SLOTS,
    time_to_minutes,
)
from apps.users.models import UserRole

PAIR_STARTS = tuple(pair_start for _, pair_start, _ in UNIVERSITY_PAIR_SLOTS)
PAIR_START_VALUES = {start.strftime("%H:%M") for start in PAIR_STARTS}
_DUTY_ALLOWED_ROLES = {
    ScheduleDutyRole.LAB_STAFF_DUTY: frozenset({UserRole.LAB_ADMIN, UserRole.LAB_HEAD}),
    ScheduleDutyRole.TEACHER_DUTY: frozenset({UserRole.TEACHER}),
}
_GRID_ENTRY_PREFETCH = ("discipline_selections__lab_works", "discipline_selections__discipline")


class ScheduleError(Exception):
    """Ошибка валидации записи расписания (показывается пользователю как есть)."""


def _parse_int(value, *, field, default=None):
    raw = (value or "").strip()
    if not raw:
        if default is None:
            raise ScheduleError(f"Заполните поле «{field}».")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ScheduleError(f"Поле «{field}» должно быть числом.") from None


def _parse_start_time(value) -> time:
    raw = (value or "").strip()
    for start in PAIR_STARTS:
        if start.strftime("%H:%M") == raw:
            return start
    raise ScheduleError("Время начала должно совпадать с началом одной из пар (08:50–17:30).")


def _clean_disciplines(laboratory, raw_ids: list[str]) -> list:
    ids = {int(item) for item in raw_ids if item.strip().isdigit()}
    if not ids:
        return []
    bound = set(
        laboratory.discipline_bindings.filter(discipline_id__in=ids).values_list(
            "discipline_id", flat=True
        )
    )
    if ids - bound:
        raise ScheduleError("Выберите дисциплины, привязанные к лаборатории.")
    return list(Discipline.objects.filter(pk__in=ids).order_by("title"))


def _clean_lab_works(laboratory, disciplines: list, raw_ids: list[str]) -> dict:
    """Явные ЛР по дисциплинам: {discipline_pk: [LabWork]}; пусто = все ЛР дисциплины."""
    ids = [item for item in raw_ids if item.strip().isdigit()]
    if not ids:
        return {}
    works = LabWork.objects.filter(pk__in=[int(item) for item in ids], laboratories=laboratory)
    by_id = {work.pk: work for work in works.prefetch_related("disciplines")}
    unknown = set(item for item in ids if int(item) not in by_id)
    if unknown:
        raise ScheduleError("Лабораторная работа не найдена в вашей лаборатории.")
    result: dict = {}
    for work in by_id.values():
        shared = work.disciplines.filter(pk__in=[d.pk for d in disciplines]).first()
        if shared is None:
            raise ScheduleError(
                f"ЛР «{work.title}» не относится к выбранным дисциплинам."
            )
        result.setdefault(shared.pk, []).append(work)
    return result


def _clean_duty_person(laboratory, duty_role: str, raw_id: str):
    if not raw_id or not raw_id.strip().isdigit():
        return None
    person = laboratory_people_qs(laboratory).filter(pk=int(raw_id)).first()
    if person is None:
        raise ScheduleError("Дежурный не найден среди людей лаборатории.")
    allowed = _DUTY_ALLOWED_ROLES.get(duty_role)
    if allowed is not None and person.role not in allowed:
        raise ScheduleError("Роль дежурного не соответствует типу дежурства.")
    return person


def clean_entry_payload(laboratory, data, *, room: Room) -> dict:
    """Валидация полей формы записи расписания; data — Mapping с get/getlist."""
    if room.laboratory_id != laboratory.pk:
        raise ScheduleError("Аудитория не принадлежит вашей лаборатории.")
    if room.is_blocked:
        raise ScheduleError("Аудитория заблокирована — расписание недоступно.")
    weekday = _parse_int(data.get("weekday"), field="День недели")
    if not 0 <= weekday <= 6:
        raise ScheduleError("День недели вне диапазона Пн–Вс.")
    start_time = _parse_start_time(data.get("start_time"))
    duration = _parse_int(data.get("duration_minutes"), field="Длительность", default=90)
    if duration not in ALLOWED_LAB_DURATIONS:
        raise ScheduleError("Длительность: 30, 45, 60 или 90 минут.")
    week_parity = data.get("week_parity") or WeekParity.BOTH
    if week_parity not in WeekParity.values:
        raise ScheduleError("Неверная чётность недели.")
    duty_role = data.get("duty_role") or ScheduleDutyRole.LAB_STAFF_DUTY
    if duty_role not in ScheduleDutyRole.values:
        raise ScheduleError("Неверный тип дежурства.")
    disciplines = _clean_disciplines(laboratory, data.getlist("disciplines"))
    if duty_role in (ScheduleDutyRole.LAB_STAFF_DUTY, ScheduleDutyRole.TEACHER_DUTY) and not disciplines:
        raise ScheduleError("Выберите хотя бы одну дисциплину.")
    works_by_discipline = _clean_lab_works(laboratory, disciplines, data.getlist("lab_works"))
    manual_title = (data.get("manual_title") or "").strip()
    if duty_role == ScheduleDutyRole.OTHER and not manual_title:
        raise ScheduleError("Для типа «Другое» заполните ручное описание.")
    duty_person = _clean_duty_person(laboratory, duty_role, data.get("duty_person") or "")
    load_group_label = (data.get("load_group_label") or "").strip()
    if duty_role != ScheduleDutyRole.TEACHER_DUTY:
        load_group_label = ""
    capacity = _parse_int(data.get("capacity"), field="Мест", default=room.capacity)
    if capacity < 1:
        raise ScheduleError("Мест: не меньше 1.")
    return {
        "room": room,
        "weekday": weekday,
        "start_time": start_time,
        "duration_minutes": duration,
        "week_parity": week_parity,
        "duty_role": duty_role,
        "disciplines": disciplines,
        "works_by_discipline": works_by_discipline,
        "manual_title": manual_title,
        "duty_person": duty_person,
        "teacher": duty_person if duty_role == ScheduleDutyRole.TEACHER_DUTY else None,
        "load_group_label": load_group_label,
        "capacity": capacity,
    }


def _apply_selections(entry: ScheduleEntry, disciplines: list, works_by_discipline: dict) -> None:
    ScheduleEntryDisciplineSelection.objects.filter(schedule_entry=entry).delete()
    for discipline in disciplines:
        selection = ScheduleEntryDisciplineSelection.objects.create(
            schedule_entry=entry, discipline=discipline
        )
        works = works_by_discipline.get(discipline.pk)
        if works:
            selection.lab_works.set(works)


def _entry_room(laboratory, data, entry: ScheduleEntry | None = None) -> Room:
    if entry is not None:
        return entry.room
    raw = (data.get("room") or "").strip()
    room = laboratory.rooms.filter(pk=int(raw) if raw.isdigit() else 0).first()
    if room is None:
        raise ScheduleError("Аудитория не найдена в вашей лаборатории.")
    return room


def create_schedule_entry(*, laboratory: Laboratory, semester: Semester, data) -> ScheduleEntry:
    room = _entry_room(laboratory, data)
    payload = clean_entry_payload(laboratory, data, room=room)
    entry = ScheduleEntry.objects.create(semester=semester, **{
        key: value for key, value in payload.items()
        if key not in ("disciplines", "works_by_discipline")
    })
    _apply_selections(entry, payload["disciplines"], payload["works_by_discipline"])
    return entry


def update_schedule_entry(*, laboratory, entry: ScheduleEntry, data) -> ScheduleEntry:
    payload = clean_entry_payload(laboratory, data, room=entry.room)
    for field, value in payload.items():
        if field in ("room", "disciplines", "works_by_discipline"):
            continue
        setattr(entry, field, value)
    entry.save()
    _apply_selections(entry, payload["disciplines"], payload["works_by_discipline"])
    # свежий инстанс с выборками: prefetch-кэш исходного entry устарел после _apply_selections
    return ScheduleEntry.objects.prefetch_related(*_GRID_ENTRY_PREFETCH).get(pk=entry.pk)


def entry_future_booking_conflicts(entry: ScheduleEntry) -> int:
    """Будущие активные записи студентов на слотах, которые осиротеют без этой записи."""
    from django.utils import timezone

    from apps.bookings.models import BookingStatus
    from apps.scheduling.models import LabSession, LabSessionStatus

    sessions = LabSession.objects.filter(
        room=entry.room,
        semester=entry.semester,
        starts_at__gt=timezone.now(),
        status__in=(LabSessionStatus.OPEN, LabSessionStatus.DRAFT),
    )
    conflicts = 0
    for session in sessions:
        if timezone.localtime(session.starts_at).weekday() != entry.weekday:
            continue
        if not entry_matches_session(entry, session):
            continue
        if any(other.pk != entry.pk for other in matching_entries_for_session(session)):
            continue
        conflicts += session.bookings.filter(current_status=BookingStatus.BOOKED).count()
    return conflicts


def delete_schedule_entry(entry: ScheduleEntry, *, force: bool) -> tuple[bool, int]:
    """Удаление с подтверждением: (False, N) — есть будущие записи студентов без
    замещающей записи; (True, 0) — удалено (осиротевшие слоты отменяет sync во view)."""
    conflicts = entry_future_booking_conflicts(entry)
    if conflicts and not force:
        return False, conflicts
    entry.delete()
    return True, 0


def _parity_overlaps(first: str, second: str) -> bool:
    return first == WeekParity.BOTH or second == WeekParity.BOTH or first == second


def _parity_matches_filter(entry_parity: str, parity_filter: str) -> bool:
    return parity_filter == WeekParity.BOTH or entry_parity == WeekParity.BOTH or entry_parity == parity_filter


def copy_schedule_weekday(
    *, room: Room, semester: Semester, source_weekday: int, target_weekday: int, parity_filter: str
) -> tuple[int, int]:
    """Копирует записи дня недели на другой день, пропуская занятые слоты.

    parity_filter — как в фильтре сетки: BOTH — все записи; ODD/EVEN — записи
    этой чётности и «каждую неделю». Возвращает (скопировано, пропущено).
    """
    if not 0 <= source_weekday <= 6 or not 0 <= target_weekday <= 6:
        raise ScheduleError("День недели вне диапазона Пн–Вс.")
    if source_weekday == target_weekday:
        raise ScheduleError("День копирования совпадает с исходным.")
    if parity_filter not in WeekParity.values:
        raise ScheduleError("Неверная чётность недели.")
    source_entries = list(
        ScheduleEntry.objects.filter(
            room=room, semester=semester, weekday=source_weekday, is_active=True
        ).prefetch_related(*_GRID_ENTRY_PREFETCH)
    )
    occupied = list(
        ScheduleEntry.objects.filter(
            room=room, semester=semester, weekday=target_weekday, is_active=True
        ).values_list("start_time", "week_parity")
    )
    created = skipped = 0
    for entry in source_entries:
        if not _parity_matches_filter(entry.week_parity, parity_filter):
            continue
        if any(
            start == entry.start_time and _parity_overlaps(parity, entry.week_parity)
            for start, parity in occupied
        ):
            skipped += 1
            continue
        copy = ScheduleEntry.objects.create(
            duty_role=entry.duty_role,
            room=entry.room,
            semester=entry.semester,
            week_parity=entry.week_parity,
            weekday=target_weekday,
            start_time=entry.start_time,
            duration_minutes=entry.duration_minutes,
            capacity=entry.capacity,
            load_group_label=entry.load_group_label,
            manual_title=entry.manual_title,
            duty_person=entry.duty_person,
            teacher=entry.teacher,
            is_active=entry.is_active,
        )
        for selection in entry.discipline_selections.all():
            clone = ScheduleEntryDisciplineSelection.objects.create(
                schedule_entry=copy, discipline=selection.discipline
            )
            clone.lab_works.set(selection.lab_works.all())
        occupied.append((copy.start_time, copy.week_parity))
        created += 1
    return created, skipped


def build_schedule_grid(entries, *, parity_filter: str = WeekParity.BOTH):
    """Сетка пары×дни для комнаты: ячейка содержит записи, начавшиеся внутри пары.

    Возвращает (rows, unplaced): rows — по паре, в каждой 7 ячеек со списком
    чипов {entry, discipline_ids, lab_work_ids} (id — строкой для data-атрибутов);
    unplaced — записи вне сетки пар (ранее созданные вручную/через админку).
    """
    rows = []
    unplaced = []
    for pair_number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        start_minutes = time_to_minutes(pair_start)
        end_minutes = time_to_minutes(pair_end)
        cells: list[list[dict]] = [[] for _ in range(7)]
        for entry in entries:
            if not _parity_matches_filter(entry.week_parity, parity_filter):
                continue
            entry_minutes = time_to_minutes(entry.start_time)
            if not start_minutes <= entry_minutes < end_minutes:
                continue
            cells[entry.weekday].append(_entry_chip(entry, pair_start))
        rows.append(
            {
                "pair": pair_number,
                "start": pair_start.strftime("%H:%M"),
                "end": pair_end.strftime("%H:%M"),
                "cells": cells,
            }
        )
    placed = {chip["entry"].pk for row in rows for cell in row["cells"] for chip in cell}
    for entry in entries:
        if entry.pk not in placed and _parity_matches_filter(entry.week_parity, parity_filter):
            unplaced.append(_entry_chip(entry))
    return rows, unplaced


def _entry_chip(entry: ScheduleEntry, pair_start: time | None = None) -> dict:
    selections = list(entry.discipline_selections.all())
    discipline_ids = [str(selection.discipline_id) for selection in selections]
    lab_work_ids = [str(work.pk) for selection in selections for work in selection.lab_works.all()]
    if entry.duty_role == ScheduleDutyRole.OTHER or not selections:
        title = entry.manual_title or "Слот"
    else:
        title = ", ".join(selection.discipline.title for selection in selections)
    parity_label = "" if entry.week_parity == WeekParity.BOTH else entry.get_week_parity_display()
    time_note = ""
    if pair_start is not None and entry.start_time != pair_start:
        time_note = f"с {entry.start_time:%H:%M}"
    return {
        "entry": entry,
        "title": title,
        "parity_label": parity_label,
        "time_note": time_note,
        "discipline_ids": ",".join(discipline_ids),
        "lab_work_ids": ",".join(dict.fromkeys(lab_work_ids)),
    }


def entries_prefetched(room: Room, semester: Semester):
    return room.schedule_entries.filter(semester=semester, is_active=True).prefetch_related(
        *_GRID_ENTRY_PREFETCH
    )
