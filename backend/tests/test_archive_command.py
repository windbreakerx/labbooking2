"""Команда archive_old_data: отчёт по умолчанию, удаление по --apply."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.bookings.models import AuditLog
from apps.scheduling.models import LabSession, LabSessionStatus

from .conftest import seed_manual_booking


def _old_cancelled_session(lab_work, room, semester, *, days=400):
    starts = timezone.now() - timedelta(days=days)
    return LabSession.objects.create(
        lab_work=lab_work,
        room=room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timedelta(minutes=90),
        status=LabSessionStatus.CANCELLED,
    )


@pytest.mark.django_db
class TestArchiveOldData:
    def test_report_does_not_delete(self, lab_work, room, semester, staff, student):
        session = _old_cancelled_session(lab_work, room, semester)
        seed_manual_booking(actor=staff, student=student, session=session)
        audit = AuditLog.objects.create(action="old", entity_type="Test", entity_id=1)
        AuditLog.objects.filter(pk=audit.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )

        output = StringIO()
        call_command("archive_old_data", stdout=output)

        assert LabSession.objects.filter(pk=session.pk).exists()
        assert AuditLog.objects.filter(pk=audit.pk).exists()
        text = output.getvalue()
        assert "Это отчёт" in text
        assert "без записей 0" in text

    def test_apply_deletes_only_sessions_without_bookings(
        self, lab_work, room, semester, staff, student
    ):
        empty = _old_cancelled_session(lab_work, room, semester)
        with_booking = _old_cancelled_session(lab_work, room, semester, days=401)
        seed_manual_booking(actor=staff, student=student, session=with_booking)
        audit = AuditLog.objects.create(action="old", entity_type="Test", entity_id=1)
        AuditLog.objects.filter(pk=audit.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )

        output = StringIO()
        call_command("archive_old_data", "--apply", stdout=output)

        assert not LabSession.objects.filter(pk=empty.pk).exists()
        assert LabSession.objects.filter(pk=with_booking.pk).exists()
        assert not AuditLog.objects.filter(pk=audit.pk).exists()
        assert "Удалено объектов" in output.getvalue()
