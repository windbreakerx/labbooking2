"""Shared pytest fixtures for booking core tests (порт из v1)."""

from datetime import datetime, time

import pytest
from django.utils import timezone

from apps.academics.models import Discipline, LabWork, LabWorkBookingRelease, Semester, StudentGroup
from apps.scheduling.models import (
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserProfile, UserRole


def create_lab_work(*disciplines, **kwargs) -> LabWork:
    lab_work = LabWork.objects.create(**kwargs)
    if disciplines:
        lab_work.disciplines.set(disciplines)
    return lab_work


def open_booking_release_for_session(session, *, lab_work=None) -> LabWorkBookingRelease:
    """Открыть запись на лабораторную работу в семестре слота (для тестов)."""
    from datetime import time as dt_time

    lab_work = lab_work or session.lab_work
    tz = timezone.get_current_timezone()
    opens_at = timezone.make_aware(
        datetime.combine(session.semester.start_date, dt_time.min), tz
    )
    release, _ = LabWorkBookingRelease.objects.get_or_create(
        lab_work=lab_work,
        semester=session.semester,
        defaults={"booking_opens_at": opens_at},
    )
    if release.booking_opens_at is None or release.booking_opens_at > timezone.now():
        release.booking_opens_at = opens_at
        release.save(update_fields=["booking_opens_at"])
    return release


def create_schedule_entry_for_session(session, *, lab_work=None, **overrides):
    """Заглушка для старого хелпера: теперь просто открывает release.

    Параметры ``overrides`` игнорируются — оставлены для совместимости тестов.
    """
    return open_booking_release_for_session(session, lab_work=lab_work)


@pytest.fixture(autouse=True)
def _disable_day_open_gate(monkeypatch):
    """Тесты ставят сессии в пределах 3 недель — горизонт 14 дней не должен им мешать."""
    monkeypatch.setattr(
        "apps.bookings.services.session_availability.is_day_open_for_booking",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "apps.bookings.services.session_catalog.is_day_open_for_booking",
        lambda *_args, **_kwargs: True,
    )


def next_open_weekday_pair(days_ahead: int = 1, hour: int = 10, minute: int = 35):
    now = timezone.now()
    tz = timezone.get_current_timezone()
    for day_offset in range(days_ahead, days_ahead + 14):
        candidate_date = (now + timezone.timedelta(days=day_offset)).date()
        if candidate_date.weekday() >= 5:
            continue
        candidate = timezone.make_aware(datetime.combine(candidate_date, time(hour, minute)), tz)
        if candidate <= now:
            continue
        return candidate
    raise RuntimeError("Не удалось подобрать открытую пару для теста.")


def far_open_weekday_pair(min_days_ahead: int = 20, hour: int = 10, minute: int = 35):
    """Будущий слот за горизонтом студенческой записи, но в будний день и на паре."""
    return next_open_weekday_pair(days_ahead=min_days_ahead, hour=hour, minute=minute)


def assign_student_group(user, student_group):
    UserProfile.objects.update_or_create(user=user, defaults={"student_group": student_group})
    return user


def make_student(email, student_group):
    return assign_student_group(
        User.objects.create_user(
            email=email,
            password="pass",  # nosec B106
            first_name=email[0].upper(),
            last_name=email.split("@")[0].title(),
            role=UserRole.STUDENT,
        ),
        student_group,
    )


def seed_manual_booking(*, actor, student, session):
    """Запись для scope/UI-тестов без прохождения правил ручной записи."""
    from apps.bookings.models import Booking, BookingStatus, RegistrationType

    discipline = session.lab_work.disciplines.order_by("title").first()
    return Booking.objects.create(
        student=student,
        lab_session=session,
        lab_work=session.lab_work,
        discipline=discipline,
        room=session.room,
        scheduled_at=session.starts_at,
        current_status=BookingStatus.BOOKED,
        registration_type=RegistrationType.MANUAL,
        registered_by=actor,
    )


@pytest.fixture
def semester(db):
    return Semester.objects.create(
        name="Test",
        start_date=timezone.now().date(),
        end_date=timezone.now().date().replace(year=timezone.now().year + 1),
        is_active=True,
    )


@pytest.fixture
def discipline(semester):
    return Discipline.objects.create(title="Физика", semester=semester, is_published=True)


@pytest.fixture
def inactive_discipline(db):
    old_sem = Semester.objects.create(
        name="Old",
        start_date=timezone.now().date().replace(year=timezone.now().year - 1),
        end_date=timezone.now().date(),
        is_active=False,
    )
    return Discipline.objects.create(title="Старая", semester=old_sem, is_published=True)


@pytest.fixture
def lab_work(discipline, room):
    lab_work = create_lab_work(
        discipline,
        number=1,
        title="ЛР 1",
        duration_minutes=90,
        capacity=2,
        is_published=True,
    )
    lab_work.laboratories.add(room.laboratory)
    return lab_work


@pytest.fixture
def room(db):
    tc, _ = TrainingCenter.objects.get_or_create(number=9001, defaults={"name": "Тестовый УЦ"})
    laboratory, _ = Laboratory.objects.get_or_create(
        training_center=tc,
        name="Тестовая лаборатория",
    )
    room, created = Room.objects.get_or_create(
        training_center=tc,
        number="101",
        defaults={"capacity": 2, "laboratory": laboratory},
    )
    if not created and room.laboratory_id is None:
        room.laboratory = laboratory
        room.save(update_fields=["laboratory"])
    return room


@pytest.fixture
def session(lab_work, room, semester):
    starts = next_open_weekday_pair(days_ahead=2, hour=10, minute=35)
    session = LabSession.objects.create(
        lab_work=lab_work,
        room=room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        status=LabSessionStatus.OPEN,
    )
    create_schedule_entry_for_session(session, lab_work=lab_work)
    return session


@pytest.fixture
def far_session(lab_work, room, semester):
    starts = far_open_weekday_pair(min_days_ahead=20)
    session = LabSession.objects.create(
        lab_work=lab_work,
        room=room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        status=LabSessionStatus.OPEN,
    )
    create_schedule_entry_for_session(session, lab_work=lab_work)
    return session


@pytest.fixture
def student_group(discipline):
    group = StudentGroup.objects.create(name="TEST-24")
    group.disciplines.add(discipline)
    return group


@pytest.fixture
def student(db, student_group):
    user = User.objects.create_user(
        email="s@stud.spmi.ru",
        password="pass",  # nosec B106
        first_name="A",
        last_name="B",
        role=UserRole.STUDENT,
    )
    UserProfile.objects.create(user=user, student_group=student_group)
    return user


@pytest.fixture
def staff(db, room):
    user = User.objects.create_user(
        email="staff@spmi.ru",
        password="pass",  # nosec B106
        first_name="S",
        last_name="T",
        role=UserRole.LAB_ADMIN,
        is_staff=True,
    )
    UserProfile.objects.create(
        user=user, training_center=room.training_center, laboratory=room.laboratory
    )
    room.default_lab_staff = user
    room.save(update_fields=["default_lab_staff"])
    return user
