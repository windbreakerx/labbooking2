"""Доступность слотов: окна записи, whitelist расписания, авто-«Посетил».

Порт из v1 (971 строк) без каскадных опций визарда — те вернутся в День 6
вместе со студенческим UI. Сетка пар и чётность недель —
scheduling/services/slot_grid.py; математика мест — seat_capacity.py,
векторизованный каталог — session_catalog.py.
"""

from collections.abc import Iterable
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from django.utils import timezone

from apps.scheduling.models import (
    AutoVisitedMode,
    Holiday,
    LaboratoryBookingSettings,
    LabSession,
    ScheduleDutyRole,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
)
from apps.scheduling.services.entry_match import entry_matches_session
from apps.scheduling.services.slot_grid import (
    BOOKING_START_GRID_MINUTES,
    UNIVERSITY_PAIR_SLOTS,
    time_to_minutes,
)

# --- Автопроставление VISITED -------------------------------------------------


def auto_visited_deadline(session_starts_at: datetime, mode: str) -> datetime | None:
    if mode == AutoVisitedMode.MANUAL:
        return None
    local_start = timezone.localtime(session_starts_at)
    tz = timezone.get_current_timezone()
    if mode == AutoVisitedMode.AT_22_00:
        return timezone.make_aware(datetime.combine(local_start.date(), time(22, 0)), tz)
    if mode == AutoVisitedMode.AT_09_00_NEXT:
        return timezone.make_aware(
            datetime.combine(local_start.date() + timedelta(days=1), time(9, 0)), tz
        )
    return None


def is_ready_for_auto_visited(
    session: LabSession,
    now: datetime | None = None,
    *,
    auto_visited_mode: str | None = None,
) -> bool:
    mode = auto_visited_mode or AutoVisitedMode.MANUAL
    if mode == AutoVisitedMode.MANUAL:
        return False
    moment = timezone.localtime(now or timezone.now())
    if session.ends_at > moment:
        return False
    deadline = auto_visited_deadline(session.starts_at, mode)
    return deadline is not None and moment >= deadline


# --- Окно записи ---------------------------------------------------------------


def _default_window_config() -> dict:
    return {
        "horizon_days": settings.BOOKING_HORIZON_DAYS,
        "cancel_hours": settings.BOOKING_CANCEL_HOURS,
        "restriction_base_time": time(9, 0),
        "restriction_hours_before_base": None,
    }


def _apply_lab_settings(config: dict, lab_settings: LaboratoryBookingSettings) -> dict:
    if lab_settings.booking_horizon_days is not None:
        config["horizon_days"] = lab_settings.booking_horizon_days
    if lab_settings.booking_cancel_hours is not None:
        config["cancel_hours"] = lab_settings.booking_cancel_hours
    config["restriction_base_time"] = lab_settings.restriction_base_time
    config["restriction_hours_before_base"] = lab_settings.restriction_hours_before_base
    return config


def booking_window_config(laboratory_id: int | None = None) -> dict:
    """Глобальные настройки окна записи с переопределениями конкретной лаборатории."""
    if not laboratory_id:
        return _default_window_config()
    lab_settings = LaboratoryBookingSettings.objects.filter(laboratory_id=laboratory_id).first()
    if not lab_settings:
        return _default_window_config()
    return _apply_lab_settings(_default_window_config(), lab_settings)


def booking_window_configs(laboratory_ids: Iterable[int | None]) -> dict[int | None, dict]:
    """Конфиги окна записи для набора лабораторий одним запросом (None = глобальный)."""
    ids = {laboratory_id for laboratory_id in laboratory_ids if laboratory_id is not None}
    configs: dict[int | None, dict] = {None: _default_window_config()}
    if not ids:
        return configs
    for lab_settings in LaboratoryBookingSettings.objects.filter(laboratory_id__in=ids):
        configs[lab_settings.laboratory_id] = _apply_lab_settings(
            _default_window_config(), lab_settings
        )
    for laboratory_id in ids - configs.keys():
        configs[laboratory_id] = _default_window_config()
    return configs


def _window_dates(config: dict, now: datetime | None = None) -> tuple:
    local_now = timezone.localtime(now or timezone.now())
    min_date = local_now.date() + timedelta(days=1)
    max_date = local_now.date() + timedelta(days=config["horizon_days"])
    return min_date, max_date


def booking_date_window(
    now: datetime | None = None,
    *,
    laboratory_id: int | None = None,
    config: dict | None = None,
) -> tuple:
    """Рабочее окно дат записи: всегда с завтрашнего дня и до горизонта N дней.

    Закрытие записи на следующий день регулируется is_before_restriction_deadline.
    ``config`` — предвычисленный конфиг (booking_window_configs), без запроса.
    """
    return _window_dates(config or booking_window_config(laboratory_id), now)


def is_day_open_for_booking(
    session_date,
    now: datetime | None = None,
    *,
    laboratory_id: int | None = None,
    config: dict | None = None,
) -> bool:
    min_date, max_date = booking_date_window(now, laboratory_id=laboratory_id, config=config)
    return min_date <= session_date <= max_date


def is_before_restriction_deadline(
    session_starts_at: datetime,
    now: datetime | None = None,
    *,
    laboratory_id: int | None = None,
    config: dict | None = None,
) -> bool:
    config = config or booking_window_config(laboratory_id)
    restriction_hours = config["restriction_hours_before_base"]
    if restriction_hours is None:
        return True
    local_now = timezone.localtime(now or timezone.now())
    local_start = timezone.localtime(session_starts_at)
    cutoff_dt = timezone.make_aware(
        datetime.combine(local_start.date(), config["restriction_base_time"]),
        timezone.get_current_timezone(),
    ) - timedelta(hours=restriction_hours)
    return local_now <= cutoff_dt


def manual_booking_max_date(now: datetime | None = None, *, holiday_dates: set | None = None):
    """Последний календарный день в пределах N рабочих недель от сегодня.

    ``holiday_dates`` — предвычисленное множество праздников (один запрос
    на страницу ручной записи вместо одного на вызов).
    """
    local_now = timezone.localtime(now or timezone.now())
    target_working_days = settings.MANUAL_BOOKING_WORKING_WEEKS * 5
    if holiday_dates is None:
        holiday_dates = set(Holiday.objects.values_list("date", flat=True))

    working_days = 0
    cursor = local_now.date()
    while working_days < target_working_days:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in holiday_dates:
            working_days += 1
    return cursor


# --- Университетские пары ------------------------------------------------------


def _pair_slot_for_time(moment: time) -> tuple[int, time, time] | None:
    minute_value = moment.hour * 60 + moment.minute
    for number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        if time_to_minutes(pair_start) <= minute_value < time_to_minutes(pair_end):
            return number, pair_start, pair_end
    return None


def is_pair_time_for_booking(session_dt: datetime) -> bool:
    """Начало слота лежит на 15-минутной сетке внутри университетской пары."""
    local_dt = timezone.localtime(session_dt)
    starts_at = local_dt.time().replace(second=0, microsecond=0)
    slot = _pair_slot_for_time(starts_at)
    if not slot:
        return False
    _, pair_start, _ = slot
    offset = time_to_minutes(starts_at) - time_to_minutes(pair_start)
    return offset >= 0 and offset % BOOKING_START_GRID_MINUTES == 0


def is_manual_session_time_allowed(session: LabSession, now: datetime | None = None) -> bool:
    """Ручная запись: будущий слот или текущая пара, если слот ещё не завершился."""
    moment = timezone.localtime(now or timezone.now())
    if session.ends_at <= moment:
        return False
    if session.starts_at > moment:
        return True
    local_start = timezone.localtime(session.starts_at)
    if local_start.date() != moment.date():
        return False
    pair_session = _pair_slot_for_time(local_start.time())
    pair_moment = _pair_slot_for_time(moment.time())
    return pair_session is not None and pair_moment is not None and pair_session[0] == pair_moment[0]


def is_session_in_manual_booking_window(
    session: LabSession,
    now: datetime | None = None,
    *,
    max_date=None,
    holiday_dates: set | None = None,
) -> bool:
    """Ручная запись: будущий слот в пределах N рабочих недель, пара, не праздник.

    ``max_date``/``holiday_dates`` считаются один раз вызывающим кодом
    (O(1) запросов на страницу вместо O(S)).
    """
    moment = now or timezone.now()
    session_local_date = timezone.localtime(session.starts_at).date()
    if max_date is None:
        max_date = manual_booking_max_date(moment)
    if session_local_date > max_date:
        return False
    if not is_manual_session_time_allowed(session, moment):
        return False
    if not is_pair_time_for_booking(session.starts_at):
        return False
    if holiday_dates is None:
        return not Holiday.objects.filter(date=session_local_date).exists()
    return session_local_date not in holiday_dates


# --- Whitelist расписания -------------------------------------------------------


def _student_group_label(student) -> str:
    try:
        profile = student.profile
    except (AttributeError, ObjectDoesNotExist):
        return ""
    if profile.student_group_id:
        return (profile.student_group.name or "").strip()
    return (profile.group_name or "").strip()


class ScheduleWhitelistIndex:
    """Активные записи расписания для набора слотов — одним блоком запросов.

    Слот видим, если совпадает с активной записью расписания (день недели,
    чётность, время в пределах записи, ЛР среди разрешённых). Записи
    TEACHER_DUTY с группой по нагрузке видны только этой группе.
    """

    def __init__(self, sessions: Iterable[LabSession]):
        self._by_slot: dict[tuple[int, int, int], list[ScheduleEntry]] = {}
        sessions = list(sessions)
        if not sessions:
            return
        selections_qs = ScheduleEntryDisciplineSelection.objects.select_related(
            "discipline"
        ).prefetch_related("lab_works", "discipline__lab_works")
        entries = (
            ScheduleEntry.objects.filter(
                is_active=True,
                room_id__in={session.room_id for session in sessions},
                semester_id__in={session.semester_id for session in sessions},
            )
            .prefetch_related(Prefetch("discipline_selections", queryset=selections_qs))
            .order_by("pk")
        )
        for entry in entries:
            self._by_slot.setdefault(
                (entry.room_id, entry.semester_id, entry.weekday), []
            ).append(entry)

    def allows(self, session: LabSession, *, student=None) -> bool:
        local_start = timezone.localtime(session.starts_at)
        candidates = self._by_slot.get(
            (session.room_id, session.semester_id, local_start.weekday()), []
        )
        matched = [entry for entry in candidates if entry_matches_session(entry, session)]
        if not matched:
            return False
        if student is None:
            return True
        unrestricted = [
            entry
            for entry in matched
            if not (entry.duty_role == ScheduleDutyRole.TEACHER_DUTY and entry.load_group_label.strip())
        ]
        if unrestricted:
            return True
        group_label = _student_group_label(student)
        restricted_groups = {
            entry.load_group_label.strip()
            for entry in matched
            if entry.duty_role == ScheduleDutyRole.TEACHER_DUTY
        }
        return bool(group_label) and group_label in restricted_groups


def session_matches_schedule_whitelist(session: LabSession, *, student=None) -> bool:
    return ScheduleWhitelistIndex([session]).allows(session, student=student)
