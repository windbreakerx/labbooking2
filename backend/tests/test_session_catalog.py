"""Векторизованный каталог: эквивалентность скалярным проверкам + константные запросы.

Замороженное время (ср 01.07.2026 14:00) даёт детерминированные окна записи;
эталон — публичные скалярные функции (те же, что проверяют пути брони).
"""

from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone

from apps.academics.models import Discipline, Semester, StudentGroup
from apps.bookings.models import BookingStatus
from apps.bookings.services.seat_capacity import session_available_seats
from apps.bookings.services.session_availability import (
    booking_date_window,
    booking_window_config,
    booking_window_configs,
    is_before_restriction_deadline,
    is_day_open_for_booking,
    is_pair_time_for_booking,
    is_session_in_manual_booking_window,
    manual_booking_max_date,
    session_matches_schedule_whitelist,
)
from apps.bookings.services.session_catalog import staff_manual_slots, student_bookable_slots
from apps.scheduling.models import (
    Holiday,
    Laboratory,
    LaboratoryBookingSettings,
    LabSession,
    LabSessionStatus,
    LabStand,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserRole

from .conftest import (
    create_lab_work,
    create_schedule_entry_for_session,
    make_student,
    seed_manual_booking,
)


@pytest.fixture
def real_catalog_day_open(monkeypatch):
    """Реальная проверка окна в каталоге (autouse-патч отключает её для дальних слотов)."""
    from apps.bookings.services import session_catalog

    monkeypatch.setattr(session_catalog, "is_day_open_for_booking", is_day_open_for_booking)


def build_catalog_world():
    """Сценарий: окна лабораторий, вместимости, стенд, whitelist, праздники, занятость."""
    tz = timezone.get_current_timezone()
    now = timezone.make_aware(datetime(2026, 7, 1, 14, 0), tz)
    tc = TrainingCenter.objects.create(number=4242, name="Каталог")

    lab_a = Laboratory.objects.create(training_center=tc, name="Лаборатория A")
    room_a = Room.objects.create(training_center=tc, number="A1", capacity=4, laboratory=lab_a)
    lab_b = Laboratory.objects.create(training_center=tc, name="Лаборатория B")
    LaboratoryBookingSettings.objects.create(
        laboratory=lab_b, booking_horizon_days=1, restriction_hours_before_base=12
    )
    room_b = Room.objects.create(training_center=tc, number="B1", capacity=4, laboratory=lab_b)
    lab_c = Laboratory.objects.create(training_center=tc, name="Лаборатория C")
    room_c = Room.objects.create(training_center=tc, number="C1", capacity=2, laboratory=lab_c)
    stand = LabStand.objects.create(
        name="Стенд каталога", inventory_number="CAT-ST-1", training_center=tc, room=room_a
    )

    semester = Semester.objects.create(
        name="Каталог", start_date=date(2026, 2, 1), end_date=date(2026, 12, 31), is_active=True
    )
    discipline = Discipline.objects.create(title="Механика", semester=semester, is_published=True)
    group = StudentGroup.objects.create(name="CAT-26")
    group.disciplines.add(discipline)

    def work(number, **kwargs):
        return create_lab_work(
            discipline,
            number=number,
            title=f"ЛР {number}",
            duration_minutes=90,
            is_published=True,
            **kwargs,
        )

    work_a = work(1, capacity=10)
    work_b = work(2, capacity=5)
    work_c1 = work(3, capacity=4)
    work_c2 = work(4)
    work_d1 = work(5, capacity=2, primary_stand=stand)
    work_d2 = work(6, capacity=2, primary_stand=stand)

    def slot(room, lab_work, day, hour, minute, *, cap=3, entry=True):
        starts = timezone.make_aware(datetime(2026, 7, day, hour, minute), tz)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=90),
            capacity=cap,
            status=LabSessionStatus.OPEN,
        )
        if entry:
            create_schedule_entry_for_session(session, lab_work=lab_work)
        return session

    s_a1 = slot(room_a, work_a, 2, 10, 35)  # чт: 2 брони, 1 место
    s_a2 = slot(room_a, work_a, 2, 12, 35, entry=False)  # без записи расписания
    s_a3 = slot(room_a, work_a, 4, 12, 35)  # суббота с расписанием
    s_full = slot(room_a, work_a, 3, 12, 35, cap=1)  # заполнен — виден ручной записи
    s_current = slot(room_a, work_a, 1, 12, 35, entry=False)  # идущая пара
    s_past = slot(room_a, work_a, 1, 10, 35, entry=False)  # завершился
    s_holiday = slot(room_a, work_a, 6, 10, 35)  # праздник 06.07
    s_far = slot(room_a, work_a, 20, 10, 35)  # за горизонтом
    s_b1 = slot(room_b, work_b, 2, 10, 35, cap=2)  # окно лаборатории B
    s_b2 = slot(room_b, work_b, 3, 10, 35, cap=2)  # за горизонтом лаборатории B
    s_busy = slot(room_b, work_b, 2, 12, 35)  # здесь бронь студента
    s_hidden = slot(room_b, work_b, 2, 12, 50)  # пересекается с ней
    s_c1 = slot(room_c, work_c1, 2, 14, 15, cap=5)  # 2 брони
    s_c2 = slot(room_c, work_c2, 2, 14, 45, cap=5)  # аудитория исчерпана пересечением
    s_d1 = slot(room_a, work_d1, 3, 10, 35, cap=2)  # 1 бронь
    s_d2 = slot(room_a, work_d2, 3, 11, 5, cap=2)  # стенд занят другой ЛР

    Holiday.objects.create(date=date(2026, 7, 6), name="День каталога")

    actor = User.objects.create_user(
        email="catalog-actor@spmi.ru", password="pass", role=UserRole.LAB_ADMIN
    )
    student = make_student("catalog-student@stud.spmi.ru", group)
    viewer = make_student("catalog-viewer@stud.spmi.ru", group)
    fill1 = make_student("catalog-fill1@stud.spmi.ru", group)
    fill2 = make_student("catalog-fill2@stud.spmi.ru", group)

    seed_manual_booking(actor=actor, student=fill1, session=s_a1)
    seed_manual_booking(actor=actor, student=fill2, session=s_a1)
    seed_manual_booking(actor=actor, student=fill1, session=s_b1)
    seed_manual_booking(actor=actor, student=fill1, session=s_c1)
    seed_manual_booking(actor=actor, student=fill2, session=s_c1)
    seed_manual_booking(actor=actor, student=fill1, session=s_full)
    seed_manual_booking(actor=actor, student=fill1, session=s_d1)
    seed_manual_booking(actor=actor, student=student, session=s_busy)

    return {
        "now": now,
        "actor": actor,
        "student": student,
        "viewer": viewer,
        "fill1": fill1,
        "room_a": room_a,
        "work_a": work_a,
        "make_slot": slot,
        "s_a1": s_a1,
        "s_a2": s_a2,
        "s_a3": s_a3,
        "s_full": s_full,
        "s_current": s_current,
        "s_past": s_past,
        "s_holiday": s_holiday,
        "s_far": s_far,
        "s_b1": s_b1,
        "s_b2": s_b2,
        "s_busy": s_busy,
        "s_hidden": s_hidden,
        "s_c1": s_c1,
        "s_c2": s_c2,
        "s_d1": s_d1,
        "s_d2": s_d2,
    }


@pytest.mark.django_db
class TestStudentCatalog:
    @pytest.mark.usefixtures("real_catalog_day_open")
    def test_catalog_matches_scalar_reference(self):
        world = build_catalog_world()
        now = world["now"]
        min_date, max_date = booking_date_window(now)
        holiday_dates = set(Holiday.objects.values_list("date", flat=True))
        base = (
            LabSession.objects.filter(
                status=LabSessionStatus.OPEN,
                starts_at__gt=now,
                starts_at__date__gte=min_date,
                starts_at__date__lte=max_date,
                room__is_blocked=False,
            )
            .exclude(starts_at__date__in=holiday_dates)
            .select_related("lab_work", "room")
            .order_by("starts_at")
        )
        results = {}
        for who_name in ("student", "viewer"):
            who = world[who_name]
            busy = list(
                who.bookings.filter(current_status=BookingStatus.BOOKED).values_list(
                    "lab_session__starts_at", "lab_session__ends_at"
                )
            )
            expected = {}
            for session in base:
                local_date = timezone.localtime(session.starts_at).date()
                if not is_pair_time_for_booking(session.starts_at):
                    continue
                if not is_day_open_for_booking(
                    local_date, now, laboratory_id=session.room.laboratory_id
                ):
                    continue
                if not is_before_restriction_deadline(
                    session.starts_at, now, laboratory_id=session.room.laboratory_id
                ):
                    continue
                if any(
                    start < session.ends_at and end > session.starts_at for start, end in busy
                ):
                    continue
                if not session_matches_schedule_whitelist(session, student=who):
                    continue
                available = session_available_seats(session)
                if available <= 0:
                    continue
                expected[session.pk] = (available, session.booked_count)
            slots = student_bookable_slots(student=who, now=now)
            results[who_name] = {
                slot.session.pk: (slot.available_seats, slot.booked_count) for slot in slots
            }
            assert results[who_name] == expected
            starts = [slot.session.starts_at for slot in slots]
            assert starts == sorted(starts)

        student_ids = set(results["student"])
        viewer_ids = set(results["viewer"])
        assert world["s_busy"].pk not in student_ids  # своя бронь
        assert world["s_hidden"].pk not in student_ids  # пересечение с ней
        assert world["s_b2"].pk not in student_ids  # горизонт лаборатории B
        assert world["s_a2"].pk not in student_ids  # нет записи расписания
        assert world["s_c2"].pk not in student_ids  # аудитория исчерпана пересечением
        assert world["s_d2"].pk not in student_ids  # стенд занят другой ЛР
        assert results["student"][world["s_a1"].pk] == (1, 2)
        assert results["viewer"][world["s_busy"].pk] == (2, 1)
        assert results["viewer"][world["s_hidden"].pk] == (3, 0)
        assert results["viewer"][world["s_c1"].pk] == (2, 2)
        assert world["s_busy"].pk in viewer_ids
        assert world["s_hidden"].pk in viewer_ids

    def test_query_count_is_constant(self, django_assert_num_queries):
        world = build_catalog_world()
        who = world["viewer"]
        who.profile.student_group  # прогрев кэшей связей студента

        with django_assert_num_queries(9):
            student_bookable_slots(student=who, now=world["now"])

        for day in (8, 9, 10):
            session = world["make_slot"](world["room_a"], world["work_a"], day, 14, 15)
            seed_manual_booking(actor=world["actor"], student=world["fill1"], session=session)

        with django_assert_num_queries(9):
            student_bookable_slots(student=who, now=world["now"])


@pytest.mark.django_db
class TestStaffManualCatalog:
    def test_slots_match_scalar_reference(self):
        world = build_catalog_world()
        now = world["now"]
        work_a = world["work_a"]
        holiday_dates = set(Holiday.objects.values_list("date", flat=True))
        max_date = manual_booking_max_date(now, holiday_dates=holiday_dates)
        base = LabSession.objects.filter(
            status=LabSessionStatus.OPEN,
            ends_at__gt=now,
            starts_at__date__lte=max_date,
            lab_work_id=work_a.pk,
            room__is_blocked=False,
        ).order_by("starts_at")
        expected = {}
        for session in base:
            if not is_session_in_manual_booking_window(
                session, now, max_date=max_date, holiday_dates=holiday_dates
            ):
                continue
            expected[session.pk] = (session_available_seats(session), session.booked_count)

        slots = staff_manual_slots(work_a.pk, now=now)
        result = {slot.session.pk: (slot.available_seats, slot.booked_count) for slot in slots}
        assert result == expected

        assert result[world["s_full"].pk] == (0, 1)  # заполненный слот доступен ручной записи
        assert result[world["s_current"].pk] == (3, 0)  # идущая пара разрешена
        assert result[world["s_a2"].pk] == (3, 0)  # whitelist для ручной записи не действует
        assert world["s_holiday"].pk not in result
        assert world["s_past"].pk not in result
        assert world["s_far"].pk not in result

    def test_query_count_is_constant(self, django_assert_num_queries):
        world = build_catalog_world()
        with django_assert_num_queries(3):
            staff_manual_slots(world["work_a"].pk, now=world["now"])
        world["make_slot"](world["room_a"], world["work_a"], 9, 14, 15)
        world["make_slot"](world["room_a"], world["work_a"], 10, 14, 15)
        with django_assert_num_queries(3):
            staff_manual_slots(world["work_a"].pk, now=world["now"])


@pytest.mark.django_db
class TestWindowConfigs:
    def test_bulk_configs_match_scalar_config(self, room):
        lab = Laboratory.objects.create(training_center=room.training_center, name="Конфиги")
        LaboratoryBookingSettings.objects.create(
            laboratory=lab,
            booking_horizon_days=5,
            booking_cancel_hours=2,
            restriction_hours_before_base=3,
        )
        plain = Laboratory.objects.create(training_center=room.training_center, name="Без настроек")

        configs = booking_window_configs({None, lab.pk, plain.pk})

        assert configs[None] == booking_window_config(None)
        assert configs[lab.pk] == booking_window_config(lab.pk)
        assert configs[plain.pk] == configs[None]
        assert configs[lab.pk]["horizon_days"] == 5
        assert configs[lab.pk]["restriction_hours_before_base"] == 3
