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
    TrainingCenter,
)
from apps.users.models import User, UserRole
from tests.conftest import (
    create_lab_work,
    make_student,
    next_open_weekday_pair,
    seed_manual_booking,
)

REPORTS_URL = "/staff/reports/"
OVERFLOW_URL = "/staff/reports/overflow/"
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

    def test_attendance_report(
        self, staff_client, staff, student, student_group, session, booking
    ):
        booking.current_status = BookingStatus.VISITED
        booking.save(update_fields=["current_status"])
        other = make_student("attendance-noshow@stud.spmi.ru", student_group)
        _booking_with_status(
            staff, other, session, BookingStatus.NO_SHOW, had_no_show=True
        )
        rows = _rows(_sheet(staff_client.get(_download_url("attendance")), "Посещаемость"))
        assert rows[0] == ["Студент", "Дисциплина", "ЛР", "Дата", "Статус", "Была неявка"]
        statuses = sorted(row[4] for row in rows[1:])
        assert statuses == ["Неявка", "Посетил"]
        assert all(row[5] == ("Да" if row[4] == "Неявка" else "Нет") for row in rows[1:])

    def test_discipline_summary_counters(
        self, staff_client, staff, student, student_group, session, booking
    ):
        statuses = [
            (BookingStatus.VISITED, {}),
            (BookingStatus.REACCESS, {}),
            (BookingStatus.CANCELLED, {"cancel_source": CancelSource.STUDENT}),
            (BookingStatus.SLOT_CANCELLED, {}),
            (BookingStatus.NO_SHOW, {"had_no_show": True}),
        ]
        for index, (status, extra) in enumerate(statuses):
            other = make_student(f"summary-{index}@stud.spmi.ru", student_group)
            _booking_with_status(staff, other, session, status, **extra)
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

    def test_overflow_report_view_and_xlsx(
        self, staff_client, staff, student, student_group, session, room
    ):
        room.capacity = 1
        room.save(update_fields=["capacity"])
        second = make_student("overflow-2@stud.spmi.ru", student_group)
        seed_manual_booking(actor=staff, student=student, session=session)
        seed_manual_booking(actor=staff, student=second, session=session)

        response = staff_client.get(OVERFLOW_URL)
        assert response.status_code == 200
        assert "ЛР 1" in response.content.decode()

        xlsx = staff_client.get(OVERFLOW_URL + "?format=xlsx")
        assert xlsx["Content-Type"].startswith(XLSX_CONTENT_TYPE)
        rows = _rows(_sheet(xlsx, "Переполненные слоты"))
        assert len(rows) == 2
        assert rows[1][4] == "ЛР 1"
        assert rows[1][5] == 2  # записано
        assert rows[1][6] == 1  # лимит аудитории

    def test_student_denied(self, client, student):
        client.force_login(student)
        response = client.get(_download_url("bookings"), follow=True)
        assert "Доступ только для сотрудников лаборатории." in response.content.decode()




@pytest.mark.django_db
def test_invalid_report_filters_do_not_500(staff_client):
    query = "?date_from=garbage&date_to=13-13-2026&discipline=abc&semester=xyz"
    for url in (
        _download_url("bookings"),
        "/staff/reports/room-load/",
        "/staff/reports/overflow/",
        "/staff/reports/teacher-load/",
        "/staff/reports/curriculum/",
    ):
        response = staff_client.get(url + query)
        assert response.status_code == 200, url
