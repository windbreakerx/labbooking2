"""Совпадение записи расписания со слотом — домен scheduling.

Перенесено из bookings/services/session_availability.py: этими функциями
пользуются whitelist каталога, генератор слотов и reconcile-функции
расписания (bookings/services/schedule_sync.py).
"""

from django.utils import timezone

from apps.scheduling.models import LabSession, ScheduleEntry, WeekParity
from apps.scheduling.services.slot_grid import academic_week_parity, time_to_minutes


def entry_lab_work_ids(entry: ScheduleEntry) -> set[int]:
    """ЛР слота: явные выборки по дисциплинам; пустая выборка = все ЛР дисциплины."""
    ids: set[int] = set()
    for selection in entry.discipline_selections.all():
        explicit = {lab_work.pk for lab_work in selection.lab_works.all()}
        if explicit:
            ids.update(explicit)
        else:
            ids.update(selection.discipline.lab_works.values_list("pk", flat=True))
    return ids


def entry_matches_session(entry: ScheduleEntry, session: LabSession) -> bool:
    local_start = timezone.localtime(session.starts_at)
    local_end = timezone.localtime(session.ends_at)
    if entry.weekday != local_start.weekday():
        return False
    if (
        entry.week_parity != WeekParity.BOTH
        and entry.week_parity != academic_week_parity(local_start.date())
    ):
        return False
    if session.lab_work_id not in entry_lab_work_ids(entry):
        return False
    entry_start_minutes = time_to_minutes(entry.start_time)
    entry_end_minutes = entry_start_minutes + entry.duration_minutes
    if time_to_minutes(local_start.time()) < entry_start_minutes:
        return False
    return time_to_minutes(local_end.time()) <= entry_end_minutes


def matching_entries_for_session(session: LabSession) -> list[ScheduleEntry]:
    """Активные записи расписания, покрывающие слот (комната/семестр/день недели)."""
    local_start = timezone.localtime(session.starts_at)
    candidates = (
        ScheduleEntry.objects.filter(
            is_active=True,
            semester_id=session.semester_id,
            room_id=session.room_id,
            weekday=local_start.weekday(),
        )
        .prefetch_related(
            "discipline_selections__lab_works",
            "discipline_selections__discipline__lab_works",
        )
        .order_by("pk")
    )
    return [entry for entry in candidates if entry_matches_session(entry, session)]
