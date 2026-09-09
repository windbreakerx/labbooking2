"""Минимальный CRUD каталога лаборатории (день 7): аудитории и лабораторные работы.

Комнаты: только базовые поля и флаги ``is_excluded_from_autogen``/``is_blocked``
(дисциплины комнаты, фото и дежурный по умолчанию — срез этапа, есть в админке).
Каскады блокировки/разблокировки и sync capacity/duration ЛР orchestrated
во view (apps/scheduling/views/catalog.py) — сервис не зависит от bookings.
"""

from apps.academics.models import ALLOWED_LAB_DURATIONS, LabWork
from apps.scheduling.models import Laboratory, Room


class CatalogError(Exception):
    """Ошибка валидации каталога (показывается пользователю как есть)."""


def _clean_room_payload(laboratory: Laboratory, data, *, room: Room | None = None):
    number = (data.get("number") or "").strip()
    if not number:
        raise CatalogError("Заполните номер аудитории.")
    name = (data.get("name") or "").strip()
    capacity_raw = (data.get("capacity") or "").strip()
    capacity = int(capacity_raw) if capacity_raw.isdigit() else 30
    if capacity < 1:
        raise CatalogError("Вместимость: не меньше 1.")
    clash = Room.objects.filter(training_center=laboratory.training_center, number=number)
    if room is not None:
        clash = clash.exclude(pk=room.pk)
    if clash.exists():
        raise CatalogError(f"Аудитория №{number} уже существует в этом учебном центре.")
    return {
        "number": number,
        "name": name,
        "capacity": capacity,
        "is_blocked": data.get("is_blocked") == "on",
        "is_excluded_from_autogen": data.get("is_excluded_from_autogen") == "on",
    }


def create_room(*, laboratory: Laboratory, data) -> Room:
    payload = _clean_room_payload(laboratory, data)
    return Room.objects.create(
        training_center=laboratory.training_center,
        laboratory=laboratory,
        **payload,
    )


def update_room(*, laboratory: Laboratory, room: Room, data) -> Room:
    if room.laboratory_id != laboratory.pk:
        raise CatalogError("Аудитория не принадлежит вашей лаборатории.")
    for field, value in _clean_room_payload(laboratory, data, room=room).items():
        setattr(room, field, value)
    room.save()
    return room


def _clean_lab_work_payload(laboratory: Laboratory, data):
    title = (data.get("title") or "").strip()
    if not title:
        raise CatalogError("Заполните название работы.")
    number_raw = (data.get("number") or "").strip()
    number = int(number_raw) if number_raw.isdigit() else 1
    if number < 1:
        raise CatalogError("Номер: не меньше 1.")
    duration_raw = (data.get("duration_minutes") or "").strip()
    duration = int(duration_raw) if duration_raw.isdigit() else 90
    if duration not in ALLOWED_LAB_DURATIONS:
        raise CatalogError("Длительность: 30, 45, 60 или 90 минут.")
    capacity_raw = (data.get("capacity") or "").strip()
    capacity = int(capacity_raw) if capacity_raw.isdigit() else 30
    if capacity < 1:
        raise CatalogError("Мест: не меньше 1.")
    discipline_ids = [item for item in data.getlist("disciplines") if item.strip().isdigit()]
    disciplines = list(
        laboratory.discipline_bindings.filter(discipline_id__in=discipline_ids)
        .select_related("discipline")
        .values_list("discipline", flat=True)
    ) if discipline_ids else []
    if not disciplines:
        raise CatalogError("Выберите хотя бы одну привязанную дисциплину.")
    return {
        "number": number,
        "title": title,
        "description": (data.get("description") or "").strip(),
        "duration_minutes": duration,
        "capacity": capacity,
        "is_published": data.get("is_published") == "on",
        "disciplines": disciplines,
    }


def create_lab_work(*, laboratory: Laboratory, data) -> LabWork:
    payload = _clean_lab_work_payload(laboratory, data)
    disciplines = payload.pop("disciplines")
    lab_work = LabWork.objects.create(**payload)
    lab_work.disciplines.set(disciplines)
    lab_work.laboratories.set([laboratory])
    lab_work.training_centers.set([laboratory.training_center])
    return lab_work


def update_lab_work(*, laboratory: Laboratory, lab_work: LabWork, data) -> LabWork:
    if not laboratory.lab_works.filter(pk=lab_work.pk).exists():
        raise CatalogError("Работа не найдена в вашей лаборатории.")
    payload = _clean_lab_work_payload(laboratory, data)
    disciplines = payload.pop("disciplines")
    for field, value in payload.items():
        setattr(lab_work, field, value)
    lab_work.save()
    lab_work.disciplines.set(disciplines)
    return lab_work


def lab_work_booking_count(lab_work: LabWork) -> int:
    """Все записи студентов по ЛР: удаление с историей запрещено (FK CASCADE)."""
    from apps.bookings.models import Booking

    return Booking.objects.filter(lab_work=lab_work).count()
