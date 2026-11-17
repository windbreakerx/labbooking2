"""Архивация старых CANCELLED-слотов и AuditLog.

Запускать раз в семестр (например, cron или однократный job).
По умолчанию удаляет записи старше 365 дней; управляется опцией ``--days``.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.bookings.models import AuditLog
from apps.scheduling.models import LabSession, LabSessionStatus


class Command(BaseCommand):
    help = "Архивировать (удалить) старые CANCELLED-слоты и записи AuditLog"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=365,
            help="Сколько дней хранить записи (по умолчанию 365).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Подсчитать, но не удалять.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)

        sessions_qs = LabSession.objects.filter(
            status=LabSessionStatus.CANCELLED,
            starts_at__lt=cutoff,
        )
        audit_qs = AuditLog.objects.filter(created_at__lt=cutoff)

        session_count = sessions_qs.count()
        audit_count = audit_qs.count()

        action = "Будет удалено" if dry_run else "Удалено"
        self.stdout.write(
            self.style.NOTICE(
                f"{action}: {session_count} CANCELLED-слотов и {audit_count} записей AuditLog "
                f"старше {days} дней (cutoff {cutoff:%Y-%m-%d %H:%M})."
            )
        )

        if not dry_run:
            sessions_qs.delete()
            audit_qs.delete()
            self.stdout.write(self.style.SUCCESS("Архивация завершена."))
