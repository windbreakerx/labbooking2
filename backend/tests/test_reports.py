"""Экспорты XLSX (день 7): отчёты по записям для стаффа и сетка расписания завлаба.

Проверки: доступ по ролям, содержимое и заголовки листов, скоуп-изоляция
лабораторий, фильтры дат/дисциплины, счётчики сводного отчёта, структура
сетки пар (включая ручные записи и чётность).
"""

from io import BytesIO

import pytest
from django.utils import timezone
from openpyxl import load_workbook

from apps.academics.models import Discipline
from apps.bookings.models import BookingStatus, CancelSource
from apps.bookings.reports import XLSX_CONTENT_TYPE
from apps.scheduling.models import (
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    ScheduleEntry,
    TrainingCenter,
)
from apps.users.models import User, UserProfile, UserRole
from tests.conftest import (
    create_lab_work,
    make_student,
    next_open_weekday_pair,
    seed_manual_booking,
)

REPORTS_URL = "/staff/reports/"
EXPORT_URL = "/lab-head/schedule/export/"
BOOKINGS_HEADER = [
    "Студент", "Email", "Группа", "Дисциплина", "ЛР", "Дата",
    "УЦ", "Ауд.", "Регистрация", "Кем записан", "Статус", "Кто отменил", "Была неявка",
]


def _download_url(report_type):
    return f"/staff/reports/{report_type}/download/"


def _sheet(response, title):
    return load_workbook(BytesIO(response.content))[title]


def _rows(ws):
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _foreign_lab(semester):
    tc2 = TrainingCenter.objects.create(number=9002, name="Чужой УЦ")
    lab2 = Laboratory.objects.create(training_center=tc2, name="Чужая лаборатория")
    room2 = Room.objects.create(training_center=tc2, number="202", capacity=2, laboratory=lab2)
    discipline2 = Discipline.objects.create(title="Чужая дисциплина", semester=semester, is_published=True)
    work2 = create_lab_work(discipline2, number=9, title="Чужая ЛР", duration_minutes=90, is_published=True)
    starts = next_open_weekday_pair(days_ahead=3)
    session2 = LabSession.objects.create(
        lab_work=work2,
        room=room2,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        capacity=2,
        status=LabSessionStatus.OPEN,
    )
    return room2, discipline2, session2


def _booking_with_status(actor, student, session, status, *, had_no_show=False, cancel_source=None):
    booking = seed_manual_booking(actor=actor, student=student, session=session)
    booking.current_status = status
    booking.had_no_show = had_no_show
    booking.cancel_source = cancel_source
    booking.save()
    return booking


def _teacher():
    return User.objects.create_user(
        email="teacher@spmi.ru", password="pass", first_name="У", last_name="Читель", role=UserRole.TEACHER
    )


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def booking(staff, student, session):
    return seed_manual_booking(actor=staff, student=student, session=session)


@pytest.mark.django_db
class TestStaffReports:
    def test_landing_shows_three_reports(self, staff_client):
        response = staff_client.get(REPORTS_URL)
        page = response.content.decode()
        assert response.status_code == 200
        for title in ("Записи на период", "Посещаемость", "Свод по дисциплине"):
            assert title in page

    def test_bookings_download_content_and_scope(self, staff_client, staff, booking, semester, student, student_group):
        foreign_student = make_student("foreign@stud.spmi.ru", student_group)
        _room2, _discipline2, session2 = _foreign_lab(semester)
        seed_manual_booking(actor=staff, student=foreign_student, session=session2)

        response = staff_client.get(_download_url("bookings"))
        assert response.status_code == 200
        assert response["Content-Type"].startswith(XLSX_CONTENT_TYPE)
        assert response["Content-Disposition"] == 'attachment; filename="bookings.xlsx"'

        rows = _rows(_sheet(response, "Записи"))
        assert rows[0] == BOOKINGS_HEADER
        assert len(rows) == 2
        row = rows[1]
        assert row[0] == "B A"
        assert row[1] == "s@stud.spmi.ru"
        assert row[2] == "TEST-24"
        assert row[3] == "Физика"
        assert row[4] == "ЛР 1"
        assert row[6] == 9001
        assert row[7] == "101"
        assert row[8] == "Не автоматическая"
        assert row[9] == "T S"
        assert row[10] == "Записан"
        assert row[11] is None
        assert row[12] == "Нет"
        assert not any("Чужая" in str(cell) for row in rows for cell in row)

    def test_bookings_date_filters(self, staff_client, booking):
        scheduled = timezone.localdate(booking.scheduled_at)
        before = _download_url("bookings") + f"?date_to={(scheduled - timezone.timedelta(days=1)).isoformat()}"
        after = _download_url("bookings") + f"?date_from={(scheduled + timezone.timedelta(days=1)).isoformat()}"
        from_day = _download_url("bookings") + f"?date_from={scheduled.isoformat()}"
        assert len(_rows(_sheet(staff_client.get(before), "Записи"))) == 1
        assert len(_rows(_sheet(staff_client.get(after), "Записи"))) == 1
        assert len(_rows(_sheet(staff_client.get(from_day), "Записи"))) == 2

    def test_discipline_filter(self, staff_client, booking, semester):
        _room2, discipline2, session2 = _foreign_lab(semester)
        response = staff_client.get(_download_url("bookings"), {"discipline": booking.discipline_id})
        rows = _rows(_sheet(response, "Записи"))
        assert len(rows) == 2
        response = staff_client.get(_download_url("bookings"), {"discipline": discipline2.pk})
        assert len(_rows(_sheet(response, "Записи"))) == 1

    def test_attendance_report(self, staff_client, staff, student, session, booking):
        _booking_with_status(staff, student, session, BookingStatus.VISITED)
        _booking_with_status(
            staff, student, session, BookingStatus.NO_SHOW, had_no_show=True
        )
        rows = _rows(_sheet(staff_client.get(_download_url("attendance")), "Посещаемость"))
        assert rows[0] == ["Студент", "Дисциплина", "ЛР", "Дата", "Статус", "Была неявка"]
        statuses = sorted(row[4] for row in rows[1:])
        assert statuses == ["Неявка", "Посетил"]
        assert all(row[5] == ("Да" if row[4] == "Неявка" else "Нет") for row in rows[1:])

    def test_discipline_summary_counters(self, staff_client, staff, student, session, booking):
        _booking_with_status(staff, student, session, BookingStatus.VISITED)
        _booking_with_status(staff, student, session, BookingStatus.REACCESS)
        _booking_with_status(
            staff, student, session, BookingStatus.CANCELLED, cancel_source=CancelSource.STUDENT
        )
        _booking_with_status(staff, student, session, BookingStatus.SLOT_CANCELLED)
        _booking_with_status(
            staff, student, session, BookingStatus.NO_SHOW, had_no_show=True
        )
        rows = _rows(_sheet(staff_client.get(_download_url("discipline_summary")), "Свод по дисциплине"))
        assert rows[0] == [
            "Дисциплина", "Записано", "Посетили", "Неявки", "Отмены",
            "Слот отменён", "Повторный доступ",
        ]
        assert rows[1] == ["Физика", 1, 1, 1, 1, 1, 1]

    def test_unknown_report_type_stub(self, staff_client):
        response = staff_client.get(_download_url("bogus"))
        assert response.status_code == 200
        assert _rows(_sheet(response, "Sheet")) == [["Неизвестный тип отчёта"]]

    def test_teacher_allowed_read_only(self, client):
        client.force_login(_teacher())
        response = client.get(_download_url("bookings"))
        assert response.status_code == 200
        assert len(_rows(_sheet(response, "Записи"))) == 1

    def test_student_denied(self, client, student):
        client.force_login(student)
        response = client.get(_download_url("bookings"), follow=True)
        assert "Доступ только для сотрудников лаборатории." in response.content.decode()


@pytest.fixture
def lab_head(room):
    user = User.objects.create_user(
        email="head@spmi.ru", password="pass", first_name="З", last_name="Лаб",
        role=UserRole.LAB_HEAD, is_staff=True,
    )
    UserProfile.objects.create(
        user=user, training_center=room.training_center, laboratory=room.laboratory
    )
    return user


@pytest.fixture
def schedule_entries(room, semester, discipline, lab_work, lab_head):
    entry = ScheduleEntry.objects.create(
        room=room, semester=semester, weekday=1, start_time="10:35", duty_person=lab_head
    )
    selection = entry.discipline_selections.create(discipline=discipline)
    selection.lab_works.set([lab_work])
    manual = ScheduleEntry.objects.create(
        room=room, semester=semester, weekday=2, start_time="12:35",
        week_parity="ODD", duty_role="OTHER", manual_title="Физпрактикум",
    )
    return entry, manual


@pytest.mark.django_db
class TestLabHeadScheduleExport:
    def test_export_returns_excel_grid(self, client, lab_head, schedule_entries):
        client.force_login(lab_head)
        response = client.get(EXPORT_URL)
        assert response.status_code == 200
        assert response["Content-Type"].startswith(XLSX_CONTENT_TYPE)
        assert response["Content-Disposition"] == 'attachment; filename="lab_head_schedule.xlsx"'

        ws = _sheet(response, "Ауд. 101")
        assert ws["A1"].value == "День / Пара"
        assert [ws.cell(row=1, column=col).value for col in range(2, 8)] == [
            "1 пара", "2 пара", "3 пара", "4 пара", "5 пара", "6 пара"
        ]
        assert [ws.cell(row=row, column=1).value for row in range(2, 9)] == [
            "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"
        ]

        pair_cell = ws.cell(row=3, column=3).value
        assert "Физика" in pair_cell
        assert "ЛР 1" in pair_cell
        assert "Каждая" in pair_cell
        assert "Лаб З" in pair_cell

        manual_cell = ws.cell(row=4, column=4).value
        assert "Физпрактикум" in manual_cell
        assert "Нечётная" in manual_cell

    def test_export_denied_for_student(self, client, student):
        client.force_login(student)
        response = client.get(EXPORT_URL, follow=True)
        assert "Доступ только для заведующего лабораторией." in response.content.decode()
