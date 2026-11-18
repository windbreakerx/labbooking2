"""Админка только для чтения и сверка счётчиков неявок."""

from io import StringIO

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory

from apps.academics.admin import DisciplineAdmin, LabWorkAdmin
from apps.academics.models import Discipline, LabWork
from apps.bookings.admin import AuditLogAdmin, BookingAdmin, BookingStatusHistoryAdmin
from apps.bookings.models import AuditLog, Booking, BookingStatusHistory, StudentLabAttendance
from apps.scheduling.admin import LabSessionAdmin
from apps.scheduling.models import LabSession

from .conftest import seed_manual_booking


@pytest.mark.django_db
def test_booking_admin_is_read_only():
    request = RequestFactory().get("/admin/")
    model_admin = site._registry[Booking]
    assert isinstance(model_admin, BookingAdmin)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.has_view_permission(request)


@pytest.mark.django_db
def test_history_and_audit_admin_are_read_only():
    request = RequestFactory().get("/admin/")
    for model, cls in (
        (BookingStatusHistory, BookingStatusHistoryAdmin),
        (AuditLog, AuditLogAdmin),
    ):
        model_admin = site._registry[model]
        assert isinstance(model_admin, cls)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request)
        assert not model_admin.has_delete_permission(request)


@pytest.mark.django_db
def test_catalog_admin_cannot_delete_history():
    request = RequestFactory().get("/admin/")
    for model, cls in (
        (LabWork, LabWorkAdmin),
        (Discipline, DisciplineAdmin),
        (LabSession, LabSessionAdmin),
    ):
        model_admin = site._registry[model]
        assert isinstance(model_admin, cls)
        assert not model_admin.has_delete_permission(request)


@pytest.mark.django_db
class TestRecountAttendance:
    def test_report_does_not_change_counter(self, student, session, staff, room):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        booking.had_no_show = True
        booking.save(update_fields=["had_no_show"])
        attendance = StudentLabAttendance.objects.create(
            student=student, laboratory=room.laboratory, no_show_count=5
        )

        output = StringIO()
        from django.core.management import call_command

        call_command("recount_attendance", stdout=output)

        attendance.refresh_from_db()
        assert attendance.no_show_count == 5
        assert "должно быть 1" in output.getvalue()
        assert "--apply" in output.getvalue()

    def test_apply_fixes_counter(self, student, session, staff, room):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        booking.had_no_show = True
        booking.save(update_fields=["had_no_show"])
        attendance = StudentLabAttendance.objects.create(
            student=student, laboratory=room.laboratory, no_show_count=5
        )

        from django.core.management import call_command

        call_command("recount_attendance", "--apply", stdout=StringIO())

        attendance.refresh_from_db()
        assert attendance.no_show_count == 1

    def test_apply_creates_missing_row(self, student, session, staff, room):
        booking = seed_manual_booking(actor=staff, student=student, session=session)
        booking.had_no_show = True
        booking.save(update_fields=["had_no_show"])

        from django.core.management import call_command

        call_command("recount_attendance", "--apply", stdout=StringIO())

        attendance = StudentLabAttendance.objects.get(
            student=student, laboratory=room.laboratory
        )
        assert attendance.no_show_count == 1
