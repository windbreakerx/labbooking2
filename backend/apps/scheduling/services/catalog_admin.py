"""CRUD каталога лаборатории: аудитории, лабораторные работы, стенды.

Комнаты: базовые поля и флаг ``is_blocked``; изменение вместимости пишется
в аудит (по ней считается лимит мест в зале). Стенды принадлежат ЛР и стоят
в конкретных аудиториях — по ним ловится параллельная пара без установки.
Каскады блокировки и sync duration ЛР выполняются во view
(apps/scheduling/views/catalog.py) — сервис не зависит от bookings.
"""

from apps.academics.models import ALLOWED_LAB_DURATIONS, LabWork, LabWorkMethodics
from apps.scheduling.models import Laboratory, LabStand, Room


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
    }


def create_room(*, laboratory: Laboratory, data) -> Room:
    payload = _clean_room_payload(laboratory, data)
    return Room.objects.create(
        training_center=laboratory.training_center,
        laboratory=laboratory,
        **payload,
    )


def update_room(
    *, laboratory: Laboratory, room: Room, data, actor=None
) -> tuple[Room, list[dict]]:
    """Сохранить аудиторию; вернуть её и слоты, где записано больше нового лимита.

    Уменьшать вместимость можно всегда: уже созданные записи не отменяются,
    но завлаб должен увидеть, какие слоты окажутся переполненными.
    """
    from apps.bookings.models import AuditLog

    if room.laboratory_id != laboratory.pk:
        raise CatalogError("Аудитория не принадлежит вашей лаборатории.")
    capacity_before = room.capacity
    for field, value in _clean_room_payload(laboratory, data, room=room).items():
        setattr(room, field, value)
    room.save()
    if room.capacity != capacity_before:
        AuditLog.objects.create(
            actor=actor,
            action="room.update",
            entity_type="Room",
            entity_id=room.pk,
            payload={"capacity_before": capacity_before, "capacity_after": room.capacity},
        )
        return room, overloaded_sessions(room)
    return room, []


def overloaded_sessions(room: Room) -> list[dict]:
    """Будущие слоты комнаты, где записано людей больше, чем мест в зале.

    Зал общий для всех пересекающихся слотов, поэтому слот показывается
    переполненным, если сумма записей по всем слотам, идущим в это время
    в комнате, больше её вместимости.
    """
    from apps.bookings.models import Booking, BookingStatus
    from apps.scheduling.models import LabSession, LabSessionStatus

    sessions = list(
        LabSession.objects.filter(room=room)
        .exclude(status=LabSessionStatus.CANCELLED)
        .select_related("lab_work")
        .order_by("starts_at")
    )
    booked_rows = Booking.objects.filter(
        current_status=BookingStatus.BOOKED, lab_session__room=room
    ).values_list("lab_session__starts_at", "lab_session__ends_at")
    result = []
    for session in sessions:
        total = sum(
            1
            for starts_at, ends_at in booked_rows
            if starts_at < session.ends_at and ends_at > session.starts_at
        )
        if total > room.capacity:
            result.append(
                {
                    "session": session,
                    "booked": total,
                    "capacity": room.capacity,
                }
            )
    return result


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
    stand_ids = [item for item in data.getlist("stands") if item.strip().isdigit()]
    stands = list(LabStand.objects.filter(pk__in=stand_ids, room__laboratory=laboratory))
    if len(stands) != len(stand_ids):
        raise CatalogError("Стенд не найден в аудиториях вашей лаборатории.")
    return {
        "number": number,
        "title": title,
        "description": (data.get("description") or "").strip(),
        "duration_minutes": duration,
        "capacity": capacity,
        "is_published": data.get("is_published") == "on",
        "disciplines": disciplines,
        "stands": stands,
    }


def capacity_exceeds_stands(capacity: int, stands) -> list[LabStand]:
    """Стенды, в которые не помещается вся группа.

    Вместимость стенда справочная: на запись она не влияет, но завлабу
    полезно знать, что установка меньше учебной группы.
    """
    return [stand for stand in stands if stand.capacity < capacity]


def create_lab_work(*, laboratory: Laboratory, data) -> LabWork:
    payload = _clean_lab_work_payload(laboratory, data)
    disciplines = payload.pop("disciplines")
    stands = payload.pop("stands")
    lab_work = LabWork.objects.create(**payload)
    lab_work.disciplines.set(disciplines)
    lab_work.stands.set(stands)
    lab_work.laboratories.set([laboratory])
    lab_work.training_centers.set([laboratory.training_center])
    return lab_work


def update_lab_work(*, laboratory: Laboratory, lab_work: LabWork, data) -> LabWork:
    if not laboratory.lab_works.filter(pk=lab_work.pk).exists():
        raise CatalogError("Работа не найдена в вашей лаборатории.")
    payload = _clean_lab_work_payload(laboratory, data)
    disciplines = payload.pop("disciplines")
    stands = payload.pop("stands")
    for field, value in payload.items():
        setattr(lab_work, field, value)
    lab_work.save()
    lab_work.disciplines.set(disciplines)
    lab_work.stands.set(stands)
    return lab_work


def archive_lab_work(lab_work: LabWork) -> None:
    """Убрать ЛР из каталога, не трогая историю записей и слотов."""
    lab_work.is_archived = True
    lab_work.is_published = False
    lab_work.save(update_fields=["is_archived", "is_published"])


def lab_work_booking_count(lab_work: LabWork) -> int:
    """Все записи студентов по ЛР: удаление с историей запрещено (FK PROTECT)."""
    from apps.bookings.models import Booking

    return Booking.objects.filter(lab_work=lab_work).count()


def add_methodic(*, laboratory: Laboratory, lab_work: LabWork, data, files, actor=None) -> LabWorkMethodics:
    """Загрузить PDF-методичку в ЛР лаборатории (имя/размер/сигнатуру проверяет модель)."""
    from django.core.exceptions import ValidationError

    from apps.bookings.models import AuditLog

    if not laboratory.lab_works.filter(pk=lab_work.pk).exists():
        raise CatalogError("Работа не принадлежит вашей лаборатории.")
    upload = files.get("file")
    if upload is None:
        raise CatalogError("Выберите PDF-файл.")

    methodic = LabWorkMethodics(
        lab_work=lab_work,
        file=upload,
        title=(data.get("title") or "").strip(),
        uploaded_by=actor,
    )
    try:
        methodic.full_clean()
    except ValidationError as exc:
        raise CatalogError(" ".join(exc.messages)) from exc
    methodic.save()
    AuditLog.objects.create(
        actor=actor,
        action="methodic.add",
        entity_type="LabWorkMethodics",
        entity_id=methodic.pk,
        payload={"lab_work_id": lab_work.pk, "file": methodic.file.name},
    )
    return methodic


def delete_methodic(*, laboratory: Laboratory, methodic: LabWorkMethodics, actor=None) -> None:
    """Удалить методичку и её файл; чужая лаборатория — ошибка."""
    from apps.bookings.models import AuditLog

    if not laboratory.lab_works.filter(pk=methodic.lab_work_id).exists():
        raise CatalogError("Методичка не принадлежит вашей лаборатории.")
    methodic_id = methodic.pk
    lab_work_id = methodic.lab_work_id
    filename = methodic.file.name
    # Файл удаляет post_delete-сигнал LabWorkMethodics.
    methodic.delete()
    AuditLog.objects.create(
        actor=actor,
        action="methodic.delete",
        entity_type="LabWorkMethodics",
        entity_id=methodic_id,
        payload={"lab_work_id": lab_work_id, "file": filename},
    )
