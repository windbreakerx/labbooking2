"""Фундамент слотов: университетская сетка пар и чётность учебных недель.

Старт слота — на 15-минутной сетке от начала пары, ЛР целиком помещается
в одну пару (пара 5 короче остальных — 85 минут). Чётность — непрерывный
счёт недель учебного года с недели, содержащей 1 сентября; разметка v2
отличается от ISO-чётности v1 — значения ODD/EVEN из v1 не переносятся.
"""

from datetime import date, time, timedelta

from apps.scheduling.models import WeekParity

UNIVERSITY_PAIR_SLOTS = [
    (1, time(8, 50), time(10, 20)),
    (2, time(10, 35), time(12, 5)),
    (3, time(12, 35), time(14, 5)),
    (4, time(14, 15), time(15, 45)),
    (5, time(15, 55), time(17, 20)),
    (6, time(17, 30), time(19, 0)),
]
BOOKING_START_GRID_MINUTES = 15


def time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def minutes_between(start: time, end: time) -> int:
    return time_to_minutes(end) - time_to_minutes(start)


def pair_start_times_for_duration(duration_minutes: int) -> list[time]:
    """Допустимые старты слота: ЛР помещается в одну пару, шаг сетки 15 минут."""
    starts: list[time] = []
    for _, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        pair_minutes = minutes_between(pair_start, pair_end)
        offset = 0
        while offset + duration_minutes <= pair_minutes:
            total_minutes = time_to_minutes(pair_start) + offset
            starts.append(time(total_minutes // 60, total_minutes % 60))
            offset += BOOKING_START_GRID_MINUTES
    return starts


def academic_week_parity(value: date) -> str:
    """Чётность учебной недели: непрерывный счёт с недели, содержащей 1 сентября.

    Неделя с 1 сентября — №1 (нечётная); счёт сквозной через каникулы и Новый
    год — весенний семестр продолжает осеннюю чётность (08.02.2027 = 24-я,
    чётная). Даты до 1 сентября относятся к прошлому учебному году. Чистая
    функция даты, без зависимости от Semester.
    """
    year = value.year if value >= date(value.year, 9, 1) else value.year - 1
    september_first = date(year, 9, 1)
    week_one_monday = september_first - timedelta(days=september_first.weekday())
    week_number = (value - week_one_monday).days // 7 + 1
    return WeekParity.ODD if week_number % 2 == 1 else WeekParity.EVEN
