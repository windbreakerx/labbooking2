"""Завлаб-UI (день 7): редактор расписания, аудитории, лабораторные работы.

Чекпоинт: create/update/delete+force/copy-day с авто-вызовом generate/sync,
парность ODD/EVEN/BOTH, дежурный из людей лабы, комнаты с флагом autogen,
sync capacity/duration ЛР, скоуп-изоляция лабораторий.
"""

from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone

from apps.academics.models import Discipline, LabWork, Semester
from apps.bookings.models import BookingStatus
from apps.scheduling.models import (
    LabDisciplineBinding,
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    ScheduleEntry,
    TrainingCenter,
    WeekParity,
)
from apps.scheduling.views import SESSION_ADMIN_LAB
from apps.users.models import User, UserProfile, UserRole
from tests.conftest import seed_manual_booking

SCHEDULE_URL = "/lab-head/schedule/"
CREATE_URL = "/lab-head/schedule/create/"
COPY_URL = "/lab-head/schedule/copy-day/"
ROOMS_URL = "/lab-head/rooms/"
LAB_WORKS_URL = "/lab-head/lab-works/"


def _update_url(pk):
    return f"/lab-head/schedule/{pk}/update/"


def _delete_url(pk):
    return f"/lab-head/schedule/{pk}/delete/"


def _user(email, role, *, laboratory=None, training_center=None):
    user = User.objects.create_user(
        email=email, password="pass", first_name="Т", last_name="Ю", role=role, is_staff=True
    )
    UserProfile.objects.create(user=user, laboratory=laboratory, training_center=training_center)
    return user


def _student(email="s@stud.spmi.ru"):
    return User.objects.create_user(email=email, password="pass", role=UserRole.STUDENT)


def _bind(lab, discipline, *, bound_by=None):
    return LabDisciplineBinding.objects.create(
        laboratory=lab, discipline=discipline, bound_by=bound_by
    )


def _room(lab, number, **kwargs):
    return Room.objects.create(
        training_center=lab.training_center, laboratory=lab, number=number, **kwargs
    )


def _lab_work(lab, discipline, **kwargs):
    kwargs.setdefault("number", 1)
    kwargs.setdefault("title", "ЛР 1")
    kwargs.setdefault("duration_minutes", 90)
    kwargs.setdefault("capacity", 30)
    work = LabWork.objects.create(**kwargs)
    work.disciplines.set([discipline])
    work.laboratories.set([lab])
    return work


def _entry_payload(room, discipline, lab_work, **overrides):
    payload = {
        "room": str(room.pk),
        "weekday": "1",
        "start_time": "10:35",
        "duration_minutes": "90",
        "week_parity": "BOTH",
        "duty_role": "LAB_STAFF_DUTY",
        "duty_person": "",
        "disciplines": [str(discipline.pk)],
        "lab_works": [str(lab_work.pk)],
        "capacity": "10",
        "load_group_label": "",
        "manual_title": "",
    }
    payload.update(overrides)
    return payload


def _next_weekday(weekday, min_days_ahead=1):
    now = timezone.now()
    for offset in range(min_days_ahead, min_days_ahead + 14):
        candidate = (now + timedelta(days=offset)).date()
        if candidate.weekday() == weekday:
            return candidate
    raise RuntimeError("Не удалось подобрать дату для дня недели.")


def _starts_at(weekday, start="10:35", min_days_ahead=1):
    hour, minute = (int(part) for part in start.split(":"))
    return timezone.make_aware(
        datetime.combine(_next_weekday(weekday, min_days_ahead), time(hour, minute)),
        timezone.get_current_timezone(),
    )


def _direct_entry(room, semester, discipline, *, weekday=1, start="10:35", **overrides):
    entry = ScheduleEntry.objects.create(
        room=room,
        semester=semester,
        weekday=weekday,
        start_time=start,
        capacity=overrides.pop("capacity", 10),
        **overrides,
    )
    selection = entry.discipline_selections.create(discipline=discipline)
    selection.lab_works.set(LabWork.objects.filter(disciplines=discipline))
    return entry


def _page(client, url):
    return client.get(url).content.decode()


@pytest.fixture
def tc(db):
    return TrainingCenter.objects.create(number=31, name="УЦ расписания")


@pytest.fixture
def lab(tc):
    return Laboratory.objects.create(training_center=tc, name="Лаборатория расписания")


@pytest.fixture
def semester(db):
    return Semester.objects.create(
        name="Семестр расписания",
        start_date=timezone.now().date(),
        end_date=timezone.now().date() + timedelta(days=180),
        is_active=True,
    )


@pytest.fixture
def room(lab):
    return _room(lab, "201", capacity=12)


@pytest.fixture
def discipline(semester):
    return Discipline.objects.create(title="Механика", semester=semester, is_published=True)


@pytest.fixture
def lab_head(lab, tc):
    return _user("head@spmi.ru", UserRole.LAB_HEAD, laboratory=lab, training_center=tc)


@pytest.fixture
def lab_admin(lab, tc):
    return _user("admin@spmi.ru", UserRole.LAB_ADMIN, laboratory=lab, training_center=tc)


@pytest.fixture
def teacher(lab, tc):
    return _user("teacher@spmi.ru", UserRole.TEACHER, laboratory=lab, training_center=tc)


@pytest.fixture
def head_client(client, lab_head):
    client.force_login(lab_head)
    return client


@pytest.fixture
def foreign_lab(db):
    other_tc = TrainingCenter.objects.create(number=32, name="Чужой УЦ")
    return Laboratory.objects.create(training_center=other_tc, name="Чужая лаборатория")


@pytest.fixture
def lab_work(lab, discipline, lab_head):
    _bind(lab, discipline, bound_by=lab_head)
    return _lab_work(lab, discipline)


@pytest.mark.django_db
class TestScheduleAccess:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get(SCHEDULE_URL, follow=True)
        assert "Запись на лабораторные" in response.content.decode()

    def test_student_denied(self, client, db):
        client.force_login(_student())
        response = client.get(SCHEDULE_URL, follow=True)
        assert "Доступ только для заведующего лабораторией." in response.content.decode()

    def test_teacher_and_lab_admin_denied(self, client, lab, tc):
        for email, role in (
            ("t@spmi.ru", UserRole.TEACHER),
            ("la@spmi.ru", UserRole.LAB_ADMIN),
        ):
            client.force_login(_user(email, role, laboratory=lab, training_center=tc))
            response = client.get(SCHEDULE_URL, follow=True)
            assert "Доступ только для заведующего лабораторией." in response.content.decode()

    def test_sysadmin_with_selected_lab_ok(self, client, lab):
        client.force_login(_user("sys@spmi.ru", UserRole.SYS_ADMIN))
        session = client.session
        session[SESSION_ADMIN_LAB] = lab.pk
        session.save()
        assert client.get(SCHEDULE_URL).status_code == 200


@pytest.mark.django_db
class TestScheduleGrid:
    def test_entry_chip_renders(self, head_client, room, semester, discipline, lab_admin):
        entry = _direct_entry(room, semester, discipline)
        entry.duty_person = lab_admin
        entry.save(update_fields=["duty_person"])
        page = _page(head_client, f"{SCHEDULE_URL}?room={room.pk}")
        assert discipline.title in page
        assert "мест: 10" in page
        assert lab_admin.header_name in page
        assert f"edit={entry.pk}" in page

    def test_parity_filter(self, head_client, room, semester, lab_head):
        odd_discipline = Discipline.objects.create(title="Нечётная дисциплина", semester=semester)
        even_discipline = Discipline.objects.create(title="Чётная дисциплина", semester=semester)
        _direct_entry(room, semester, odd_discipline, week_parity=WeekParity.ODD)
        _direct_entry(room, semester, even_discipline, week_parity=WeekParity.EVEN)
        page = _page(head_client, f"{SCHEDULE_URL}?room={room.pk}&parity=ODD")
        assert odd_discipline.title in page
        assert even_discipline.title not in page

    def test_room_selector_scopes_entries(self, head_client, lab, semester, discipline):
        room_a = _room(lab, "101")
        room_b = _room(lab, "102")
        _direct_entry(room_a, semester, discipline)
        assert discipline.title not in _page(head_client, f"{SCHEDULE_URL}?room={room_b.pk}")
        assert discipline.title in _page(head_client, f"{SCHEDULE_URL}?room={room_a.pk}")

    def test_no_active_semester_shows_hint(self, head_client, room):
        assert "Нет активного семестра" in _page(head_client, SCHEDULE_URL)

    def test_no_rooms_shows_hint(self, head_client, lab, semester):
        assert "нет аудиторий" in _page(head_client, SCHEDULE_URL)

    def test_free_cell_links_to_create(self, head_client, room, semester):
        assert "new=1-10:35" in _page(head_client, f"{SCHEDULE_URL}?room={room.pk}")

    def test_edit_param_renders_form_and_conflicts(
        self, head_client, room, semester, discipline, lab_work, lab_head, lab_admin, teacher
    ):
        entry = _direct_entry(room, semester, discipline)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=_starts_at(1),
            ends_at=_starts_at(1) + timedelta(minutes=90),
            capacity=10,
        )
        seed_manual_booking(actor=lab_head, student=_student(), session=session)
        page = _page(head_client, f"{SCHEDULE_URL}?room={room.pk}&edit={entry.pk}")
        assert _update_url(entry.pk) in page
        assert _delete_url(entry.pk) in page
        assert "сейчас их: 1" in page
        # селект дежурного — люди лаборатории (queryset), включая преподавателя
        assert lab_admin.header_name in page
        assert teacher.header_name in page


@pytest.mark.django_db
class TestScheduleCreate:
    def test_create_generates_future_session(self, head_client, room, semester, discipline, lab_work):
        response = head_client.post(
            CREATE_URL, _entry_payload(room, discipline, lab_work), follow=True
        )
        assert "создана" in response.content.decode()
        entry = ScheduleEntry.objects.get(room=room, weekday=1, start_time="10:35")
        assert entry.semester == semester
        assert entry.duty_person is None and entry.teacher is None
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        assert session.room == room
        assert session.capacity == 10
        assert session.status == LabSessionStatus.OPEN

    def test_create_teacher_duty_sets_teacher(
        self, head_client, room, semester, discipline, lab_work, teacher
    ):
        head_client.post(
            CREATE_URL,
            _entry_payload(
                room, discipline, lab_work, duty_role="TEACHER_DUTY", duty_person=str(teacher.pk)
            ),
        )
        entry = ScheduleEntry.objects.get(room=room)
        assert entry.duty_person == teacher
        assert entry.teacher == teacher
        assert LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1)).teacher == teacher

    def test_create_without_explicit_works_whitelists_all(
        self, head_client, room, semester, discipline, lab_work
    ):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work, lab_works=[]))
        selection = ScheduleEntry.objects.get(room=room).discipline_selections.get()
        assert selection.lab_works.count() == 0  # пустая выборка = все ЛР дисциплины
        assert LabSession.objects.filter(lab_work=lab_work, starts_at=_starts_at(1)).exists()

    def test_create_in_excluded_room_generates_nothing(
        self, head_client, lab, semester, discipline, lab_work
    ):
        excluded = _room(lab, "211", is_excluded_from_autogen=True)
        head_client.post(CREATE_URL, _entry_payload(excluded, discipline, lab_work), follow=True)
        assert ScheduleEntry.objects.filter(room=excluded).exists()
        assert LabSession.objects.filter(room=excluded).count() == 0

    def test_create_in_blocked_room_denied(self, head_client, lab, semester, discipline, lab_work):
        blocked = _room(lab, "211", is_blocked=True)
        response = head_client.post(
            CREATE_URL, _entry_payload(blocked, discipline, lab_work), follow=True
        )
        assert "заблокирована" in response.content.decode()
        assert not ScheduleEntry.objects.exists()

    def test_create_rejects_foreign_room(
        self, head_client, foreign_lab, semester, discipline, lab_work
    ):
        foreign_room = _room(foreign_lab, "301")
        response = head_client.post(
            CREATE_URL, _entry_payload(foreign_room, discipline, lab_work), follow=True
        )
        assert "не найдена в вашей лаборатории" in response.content.decode()
        assert not ScheduleEntry.objects.exists()

    def test_create_validation_errors(self, head_client, room, semester, discipline, lab_work, teacher):
        cases = [
            (_entry_payload(room, discipline, lab_work, weekday="9"), "День недели"),
            (_entry_payload(room, discipline, lab_work, start_time="09:05"), "началом одной из пар"),
            (_entry_payload(room, discipline, lab_work, duration_minutes="40"), "Длительность"),
            (_entry_payload(room, discipline, lab_work, duty_role="OTHER", manual_title=""), "ручное описание"),
            (_entry_payload(room, discipline, lab_work, duty_person=str(teacher.pk)), "не соответствует"),
            (_entry_payload(room, discipline, lab_work, disciplines=[]), "одну дисциплину"),
        ]
        for payload, expected in cases:
            response = head_client.post(CREATE_URL, payload, follow=True)
            assert expected in response.content.decode(), payload
        assert ScheduleEntry.objects.count() == 0

    def test_create_rejects_unbound_discipline_and_foreign_work(
        self, head_client, room, semester, discipline, lab_work, foreign_lab
    ):
        unbound = Discipline.objects.create(title="Не привязана", semester=semester)
        response = head_client.post(
            CREATE_URL,
            _entry_payload(room, discipline, lab_work, disciplines=[str(unbound.pk)]),
            follow=True,
        )
        assert "привязанные к лаборатории" in response.content.decode()
        foreign_work = LabWork.objects.create(title="Чужая", duration_minutes=90)
        foreign_work.disciplines.set([discipline])
        foreign_work.laboratories.set([foreign_lab])
        response = head_client.post(
            CREATE_URL,
            _entry_payload(room, discipline, lab_work, lab_works=[str(foreign_work.pk)]),
            follow=True,
        )
        assert "не найдена в вашей лаборатории" in response.content.decode()
        assert ScheduleEntry.objects.count() == 0

    def test_create_without_active_semester_denied(
        self, head_client, room, discipline, lab_work
    ):
        Semester.objects.update(is_active=False)
        response = head_client.post(
            CREATE_URL, _entry_payload(room, discipline, lab_work), follow=True
        )
        assert "Нет активного семестра" in response.content.decode()
        assert not ScheduleEntry.objects.exists()


@pytest.mark.django_db
class TestScheduleUpdate:
    @pytest.fixture
    def prepared(self, head_client, room, discipline, lab_work):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        return ScheduleEntry.objects.get(room=room)

    def test_update_syncs_capacity_and_teacher(
        self, head_client, room, discipline, lab_work, teacher, prepared
    ):
        other_teacher = _user("t2@spmi.ru", UserRole.TEACHER, laboratory=room.laboratory)
        response = head_client.post(
            _update_url(prepared.pk),
            _entry_payload(
                room,
                discipline,
                lab_work,
                capacity="5",
                duty_role="TEACHER_DUTY",
                duty_person=str(other_teacher.pk),
            ),
            follow=True,
        )
        assert "обновлена" in response.content.decode()
        prepared.refresh_from_db()
        assert prepared.capacity == 5 and prepared.teacher == other_teacher
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        assert session.capacity == 5
        assert session.teacher == other_teacher

    def test_update_moves_weekday_cancels_old_sessions(
        self, head_client, room, discipline, lab_work, lab_head, prepared
    ):
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        booking = seed_manual_booking(actor=lab_head, student=_student(), session=session)
        head_client.post(_update_url(prepared.pk), _entry_payload(room, discipline, lab_work, weekday="2"))
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.SLOT_CANCELLED
        assert LabSession.objects.filter(lab_work=lab_work, starts_at=_starts_at(2)).exists()

    def test_update_does_not_resurrect_cancelled(
        self, head_client, room, discipline, lab_work, prepared
    ):
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        session.status = LabSessionStatus.CANCELLED
        session.save(update_fields=["status"])
        head_client.post(_update_url(prepared.pk), _entry_payload(room, discipline, lab_work, capacity="7"))
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        assert session.capacity == 10  # отменённые не синхронизируются

    def test_update_foreign_entry_denied(self, head_client, foreign_lab, semester, discipline):
        foreign_room = _room(foreign_lab, "301")
        entry = _direct_entry(foreign_room, semester, discipline)
        response = head_client.post(
            _update_url(entry.pk),
            _entry_payload(foreign_room, discipline, _lab_work(foreign_lab, discipline)),
            follow=True,
        )
        assert "не найдена" in response.content.decode()
        entry.refresh_from_db()
        assert entry.capacity == 10


@pytest.mark.django_db
class TestScheduleDelete:
    @pytest.fixture
    def prepared(self, head_client, room, discipline, lab_work, lab_head):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        entry = ScheduleEntry.objects.get(room=room)
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        booking = seed_manual_booking(actor=lab_head, student=_student(), session=session)
        return entry, session, booking

    def test_delete_requires_confirm_when_bookings(self, head_client, prepared):
        entry, session, booking = prepared
        response = head_client.post(_delete_url(entry.pk), follow=True)
        assert "подтвердите удаление" in response.content.decode()
        assert ScheduleEntry.objects.filter(pk=entry.pk).exists()
        session.refresh_from_db()
        assert session.status == LabSessionStatus.OPEN
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.BOOKED

    def test_delete_force_cancels_orphans(self, head_client, prepared):
        entry, session, booking = prepared
        response = head_client.post(_delete_url(entry.pk), {"confirm": "yes"}, follow=True)
        assert "удалена" in response.content.decode()
        assert not ScheduleEntry.objects.filter(pk=entry.pk).exists()
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        booking.refresh_from_db()
        assert booking.current_status == BookingStatus.SLOT_CANCELLED

    def test_delete_without_bookings_removes_quietly(
        self, head_client, room, discipline, lab_work
    ):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        entry = ScheduleEntry.objects.get(room=room)
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        response = head_client.post(_delete_url(entry.pk), follow=True)
        assert "удалена" in response.content.decode()
        session.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED

    def test_delete_force_keeps_sessions_covered_by_sibling(
        self, head_client, room, discipline, lab_work, lab_head
    ):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        booking = seed_manual_booking(actor=lab_head, student=_student(), session=session)
        first = ScheduleEntry.objects.filter(room=room).order_by("pk").first()
        head_client.post(_delete_url(first.pk), {"confirm": "yes"})
        session.refresh_from_db()
        booking.refresh_from_db()
        assert session.status == LabSessionStatus.OPEN  # вторая запись всё ещё покрывает слот
        assert booking.current_status == BookingStatus.BOOKED


@pytest.mark.django_db
class TestCopyDay:
    def test_copy_duplicates_entries(self, head_client, room, semester, discipline, lab_work):
        _direct_entry(room, semester, discipline, weekday=1, week_parity=WeekParity.ODD)
        _direct_entry(room, semester, discipline, weekday=1, start="12:35", week_parity=WeekParity.BOTH)
        response = head_client.post(
            COPY_URL,
            {"room": str(room.pk), "source_weekday": "1", "target_weekday": "3", "week_parity": "BOTH"},
            follow=True,
        )
        assert "Скопировано записей: 2" in response.content.decode()
        assert ScheduleEntry.objects.filter(room=room, weekday=3).count() == 2

    def test_copy_skips_occupied_slots(self, head_client, room, semester, discipline, lab_work):
        _direct_entry(room, semester, discipline, weekday=1)
        _direct_entry(room, semester, discipline, weekday=3)
        response = head_client.post(
            COPY_URL,
            {"room": str(room.pk), "source_weekday": "1", "target_weekday": "3", "week_parity": "BOTH"},
            follow=True,
        )
        assert "пропущено занятых слотов: 1" in response.content.decode()
        assert ScheduleEntry.objects.filter(room=room, weekday=3).count() == 1

    def test_copy_parity_filter(self, head_client, room, semester, discipline, lab_work):
        _direct_entry(room, semester, discipline, weekday=1, week_parity=WeekParity.ODD)
        _direct_entry(room, semester, discipline, weekday=1, week_parity=WeekParity.EVEN)
        head_client.post(
            COPY_URL,
            {"room": str(room.pk), "source_weekday": "1", "target_weekday": "4", "week_parity": "ODD"},
        )
        copied = ScheduleEntry.objects.filter(room=room, weekday=4)
        assert copied.count() == 1
        assert copied.get().week_parity == WeekParity.ODD

    def test_copy_generates_sessions_for_target(
        self, head_client, room, semester, discipline, lab_work
    ):
        _direct_entry(room, semester, discipline, weekday=1)
        head_client.post(
            COPY_URL,
            {"room": str(room.pk), "source_weekday": "1", "target_weekday": "3", "week_parity": "BOTH"},
        )
        assert LabSession.objects.filter(lab_work=lab_work, starts_at=_starts_at(3)).exists()

    def test_copy_same_day_denied(self, head_client, room, semester, discipline, lab_work):
        _direct_entry(room, semester, discipline, weekday=1)
        response = head_client.post(
            COPY_URL,
            {"room": str(room.pk), "source_weekday": "1", "target_weekday": "1", "week_parity": "BOTH"},
            follow=True,
        )
        assert "совпадает" in response.content.decode()


@pytest.mark.django_db
class TestRoomsUI:
    def test_page_lists_only_own_rooms(self, head_client, room, foreign_lab):
        _room(foreign_lab, "999")
        page = _page(head_client, ROOMS_URL)
        assert room.number in page
        assert "999" not in page

    def test_create_room(self, head_client, lab):
        response = head_client.post(
            f"{ROOMS_URL}create/",
            {"number": "202", "name": "Физпрактикум", "capacity": "15"},
            follow=True,
        )
        assert "создана" in response.content.decode()
        room = Room.objects.get(training_center=lab.training_center, number="202")
        assert room.laboratory == lab
        assert room.capacity == 15
        assert room.is_blocked is False and room.is_excluded_from_autogen is False

    def test_create_duplicate_number_denied(self, head_client, lab, room):
        response = head_client.post(
            f"{ROOMS_URL}create/", {"number": room.number, "capacity": "10"}, follow=True
        )
        assert "уже существует" in response.content.decode()
        assert lab.rooms.count() == 1

    def test_update_flags(self, head_client, room):
        response = head_client.post(
            f"{ROOMS_URL}{room.pk}/update/",
            {"number": room.number, "capacity": "12", "is_excluded_from_autogen": "on"},
            follow=True,
        )
        assert "обновлена" in response.content.decode()
        room.refresh_from_db()
        assert room.is_excluded_from_autogen is True
        assert room.is_blocked is False

    def test_block_room_cancels_future_sessions(
        self, head_client, room, discipline, lab_work, lab_head
    ):
        head_client.post(CREATE_URL, _entry_payload(room, discipline, lab_work))
        session = LabSession.objects.get(lab_work=lab_work, starts_at=_starts_at(1))
        booking = seed_manual_booking(actor=lab_head, student=_student(), session=session)
        response = head_client.post(
            f"{ROOMS_URL}{room.pk}/update/",
            {"number": room.number, "capacity": "12", "is_blocked": "on"},
            follow=True,
        )
        assert "заблокирована" in response.content.decode()
        session.refresh_from_db()
        booking.refresh_from_db()
        assert session.status == LabSessionStatus.CANCELLED
        assert booking.current_status == BookingStatus.SLOT_CANCELLED

    def test_unblock_room_regenerates(self, head_client, lab, semester, discipline, lab_work):
        blocked = _room(lab, "203", is_blocked=True)
        _direct_entry(blocked, semester, discipline)
        assert LabSession.objects.filter(room=blocked).count() == 0
        head_client.post(f"{ROOMS_URL}{blocked.pk}/update/", {"number": "203", "capacity": "12"})
        assert LabSession.objects.filter(room=blocked, starts_at=_starts_at(1)).exists()

    def test_update_foreign_room_denied(self, head_client, foreign_lab):
        foreign_room = _room(foreign_lab, "301")
        response = head_client.post(
            f"{ROOMS_URL}{foreign_room.pk}/update/",
            {"number": "301", "capacity": "10"},
            follow=True,
        )
        assert "не найдена" in response.content.decode()
        foreign_room.refresh_from_db()
        assert foreign_room.capacity == 30


@pytest.mark.django_db
class TestLabWorksUI:
    def test_page_lists_only_own_works(self, head_client, lab_work, foreign_lab):
        foreign_work = LabWork.objects.create(title="Чужая работа", duration_minutes=90)
        foreign_work.laboratories.set([foreign_lab])
        page = _page(head_client, LAB_WORKS_URL)
        assert lab_work.title in page
        assert foreign_work.title not in page

    def test_create_work(self, head_client, lab, discipline, lab_head):
        _bind(lab, discipline, bound_by=lab_head)
        response = head_client.post(
            f"{LAB_WORKS_URL}create/",
            {
                "number": "2",
                "title": "Маятник",
                "disciplines": [str(discipline.pk)],
                "duration_minutes": "45",
                "capacity": "6",
                "is_published": "on",
            },
            follow=True,
        )
        assert "создана" in response.content.decode()
        work = LabWork.objects.get(title="Маятник")
        assert work.disciplines.first() == discipline
        assert work.laboratories.first() == lab
        assert work.training_centers.first() == lab.training_center

    def test_create_requires_discipline(self, head_client):
        response = head_client.post(
            f"{LAB_WORKS_URL}create/", {"title": "Без дисциплины"}, follow=True
        )
        assert "дисциплину" in response.content.decode()
        assert LabWork.objects.count() == 0

    def test_update_capacity_syncs_sessions(
        self, head_client, room, semester, discipline, lab_work
    ):
        _direct_entry(room, semester, discipline)
        LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=_starts_at(1),
            ends_at=_starts_at(1) + timedelta(minutes=90),
            capacity=10,
        )
        response = head_client.post(
            f"{LAB_WORKS_URL}{lab_work.pk}/update/",
            {
                "number": "1",
                "title": lab_work.title,
                "disciplines": [str(discipline.pk)],
                "duration_minutes": "90",
                "capacity": "3",
                "is_published": "on",
            },
            follow=True,
        )
        assert "вместимость синхронизирована" in response.content.decode()
        assert LabSession.objects.get(lab_work=lab_work).capacity == 3  # min(запись=10, ЛР=3)

    def test_update_duration_syncs_sessions(
        self, head_client, room, semester, discipline, lab_work
    ):
        _direct_entry(room, semester, discipline)
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=_starts_at(1),
            ends_at=_starts_at(1) + timedelta(minutes=90),
            capacity=10,
        )
        head_client.post(
            f"{LAB_WORKS_URL}{lab_work.pk}/update/",
            {
                "number": "1",
                "title": lab_work.title,
                "disciplines": [str(discipline.pk)],
                "duration_minutes": "60",
                "capacity": "30",
                "is_published": "on",
            },
        )
        session.refresh_from_db()
        assert session.ends_at == session.starts_at + timedelta(minutes=60)

    def test_delete_blocked_with_bookings(
        self, head_client, room, semester, lab_work, lab_head
    ):
        session = LabSession.objects.create(
            lab_work=lab_work,
            room=room,
            semester=semester,
            starts_at=_starts_at(1),
            ends_at=_starts_at(1) + timedelta(minutes=90),
            capacity=10,
        )
        seed_manual_booking(actor=lab_head, student=_student(), session=session)
        response = head_client.post(f"{LAB_WORKS_URL}{lab_work.pk}/delete/", follow=True)
        assert "удаление запрещено" in response.content.decode()
        assert LabWork.objects.filter(pk=lab_work.pk).exists()

    def test_delete_without_bookings(self, head_client, lab_work):
        response = head_client.post(f"{LAB_WORKS_URL}{lab_work.pk}/delete/", follow=True)
        assert "удалена" in response.content.decode()
        assert not LabWork.objects.filter(pk=lab_work.pk).exists()

    def test_update_foreign_work_denied(self, head_client, foreign_lab, discipline):
        foreign_work = LabWork.objects.create(title="Чужая работа", duration_minutes=90)
        foreign_work.laboratories.set([foreign_lab])
        response = head_client.post(
            f"{LAB_WORKS_URL}{foreign_work.pk}/update/",
            {
                "number": "1",
                "title": "Взлом",
                "disciplines": [str(discipline.pk)],
                "duration_minutes": "90",
                "capacity": "30",
            },
            follow=True,
        )
        assert "не найдена в вашей лаборатории" in response.content.decode()
        foreign_work.refresh_from_db()
        assert foreign_work.title == "Чужая работа"
