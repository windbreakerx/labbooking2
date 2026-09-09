"""Генерация слотов LabSession из записей расписания — insert-only.

Естественный ключ (lab_work, room, semester, starts_at) защищён констрейнтом
scheduling_labsession_unique_slot: повторные запуски не плодят дубли и
никогда не ре-открывают отменённые слоты («зомби» v1, где update_or_create
возвращал status=OPEN). Изменение существующих слотов — только reconcile
(bookings/services/schedule_sync.py). Слоты создаются исключительно из
записей расписания; fallback-генерация v1 (будни в аудитории по умолчанию
для ЛР без расписания) срезана.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from apps.academics.models import ALLOWED_LAB_DURATIONS, LabWork, Semester
from apps.scheduling.models import (
    Holiday,
    LabSession,
    LabSessionStatus,
    ScheduleEntry,
    WeekParity,
)
from apps.scheduling.services.entry_match import entry_lab_work_ids
from apps.scheduling.services.slot_grid import (
    academic_week_parity,
    pair_start_times_for_duration,
)

_ENTRY_PREFETCH = (
    "discipline_selections__lab_works",
    "discipline_selections__discipline__lab_works",
)


def generated_session_capacity(entry: ScheduleEntry, lab_work: LabWork) -> int:
    """Вместимость сгенерированного слота: минимум записи расписания и ЛР.

    Аудитория в формулу не входит: она ограничивает слот динамически, только
    когда в ней есть записи на пересекающихся слотах (seat_capacity).
    """
    return min(entry.capacity, lab_work.capacity)


def generate_lab_sessions(
    *,
    semester: Semester,
    weeks: int | None = None,
    now: datetime | None = None,
    lab_work_ids: list[int] | None = None,
) -> int:
    """Создаёт будущие слоты по активным записям расписания в пределах горизонта.

    Возвращает число созданных слотов (существующие, включая отменённые,
    пропускаются констрейнтом — считаем разницей до/после).
    """
    local_now = timezone.localtime(now or timezone.now())
    today = local_now.date()
    days_ahead = max((weeks or 2) * 7, settings.BOOKING_HORIZON_DAYS + 1)
    tz = timezone.get_current_timezone()

    entries = list(
        ScheduleEntry.objects.filter(
            is_active=True,
            semester=semester,
            room__is_blocked=False,
            room__is_excluded_from_autogen=False,
        ).prefetch_related(*_ENTRY_PREFETCH)
    )
    all_ids: set[int] = set()
    for entry in entries:
        all_ids.update(entry_lab_work_ids(entry))
    published: dict[int, LabWork] = {}
    if all_ids:
        for lab_work in LabWork.objects.filter(pk__in=all_ids, is_published=True):
            published[lab_work.pk] = lab_work
    if lab_work_ids is not None:
        requested = set(lab_work_ids)
        published = {pk: lw for pk, lw in published.items() if pk in requested}

    prepared: list[tuple[ScheduleEntry, list[LabWork]]] = []
    for entry in entries:
        works = [published[pk] for pk in entry_lab_work_ids(entry) if pk in published]
        if works:
            prepared.append((entry, works))

    holidays = set(Holiday.objects.values_list("date", flat=True))
    starts_by_duration: dict[int, list[time]] = {
        duration: pair_start_times_for_duration(duration) for duration in ALLOWED_LAB_DURATIONS
    }

    to_create: list[LabSession] = []
    seen: set[tuple[int, int, int, datetime]] = set()
    for day_offset in range(1, days_ahead + 1):
        session_date = today + timedelta(days=day_offset)
        if session_date in holidays:
            continue
        weekday = session_date.weekday()
        for entry, works in prepared:
            if entry.weekday != weekday:
                continue
            if (
                entry.week_parity != WeekParity.BOTH
                and entry.week_parity != academic_week_parity(session_date)
            ):
                continue
            entry_starts_at = timezone.make_aware(datetime.combine(session_date, entry.start_time), tz)
            entry_ends_at = entry_starts_at + timedelta(minutes=entry.duration_minutes)
            if entry_ends_at <= local_now:
                continue
            for lab_work in works:
                for start_time in starts_by_duration[lab_work.duration_minutes]:
                    starts_at = timezone.make_aware(datetime.combine(session_date, start_time), tz)
                    if starts_at < entry_starts_at:
                        continue
                    ends_at = starts_at + timedelta(minutes=lab_work.duration_minutes)
                    if ends_at > entry_ends_at:
                        break  # старты возрастают, дальше — все за пределами записи
                    if starts_at <= local_now:
                        continue
                    key = (lab_work.pk, entry.room_id, semester.pk, starts_at)
                    if key in seen:
                        continue
                    seen.add(key)
                    to_create.append(
                        LabSession(
                            lab_work=lab_work,
                            room=entry.room,
                            semester=semester,
                            teacher=entry.teacher,
                            starts_at=starts_at,
                            ends_at=ends_at,
                            capacity=generated_session_capacity(entry, lab_work),
                            status=LabSessionStatus.OPEN,
                        )
                    )
    if not to_create:
        return 0
    before = LabSession.objects.filter(semester=semester).count()
    LabSession.objects.bulk_create(to_create, ignore_conflicts=True)
    return LabSession.objects.filter(semester=semester).count() - before
