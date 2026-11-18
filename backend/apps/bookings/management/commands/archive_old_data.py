"""Уборка старых отменённых слотов и записей аудита.

По умолчанию команда только считает и показывает, что будет удалено.
Удаление выполняется с ``--apply``. Слоты, у которых есть записи студентов
или история статусов, не удаляются: после перехода ``Booking.lab_session``
на ``PROTECT`` это ещё и запрещено на уровне базы.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.bookings.models import AuditLog, Booking
from apps.scheduling.models import LabSession, LabSessionStatus


class Command(BaseCommand):
    help = "Показать и (с --apply) удалить старые CANCELLED-слоты без истории и записи AuditLog"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=365,
            help="Сколько дней хранить записи (по умолчанию 365).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Выполнить удаление. Без флага команда только показывает отчёт.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        apply_changes = options["apply"]
        cutoff = timezone.now() - timedelta(days=days)

        old_sessions = LabSession.objects.filter(
            status=LabSessionStatus.CANCELLED,
            starts_at__lt=cutoff,
        )
        deletable_sessions = old_sessions.annotate(
            has_bookings=Exists(Booking.objects.filter(lab_session=OuterRef("pk")))
        ).filter(has_bookings=False)
        audit_qs = AuditLog.objects.filter(created_at__lt=cutoff)

        total_sessions = old_sessions.count()
        deletable_count = deletable_sessions.count()
        audit_count = audit_qs.count()
        kept_count = total_sessions - deletable_count

        self.stdout.write(
            self.style.NOTICE(
                f"Старше {days} дней (до {cutoff:%Y-%m-%d %H:%M}): "
                f"отменённых слотов {total_sessions}, из них без записей {deletable_count}, "
                f"останутся с историей {kept_count}; записей аудита {audit_count}."
            )
        )

        if not apply_changes:
            self.stdout.write(
                "Это отчёт. Для удаления запустите команду с --apply."
            )
            return

        deleted_sessions = deletable_sessions.delete()[0]
        deleted_audit = audit_qs.delete()[0]
        self.stdout.write(
            self.style.SUCCESS(
                f"Удалено объектов: слотов и связанных записей {deleted_sessions}, аудита {deleted_audit}."
            )
        )
