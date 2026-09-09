"""Разовая генерация будущих слотов по расписанию активного семестра (cron)."""

from django.core.management.base import BaseCommand, CommandError

from apps.academics.models import Semester
from apps.scheduling.services.slot_generation import generate_lab_sessions


class Command(BaseCommand):
    help = "Создаёт будущие слоты лабораторных работ по записям расписания активного семестра."

    def add_arguments(self, parser):
        parser.add_argument(
            "--weeks",
            type=int,
            default=2,
            help="Горизонт генерации в неделях (не меньше горизонта записи).",
        )

    def handle(self, *args, **options):
        semester = Semester.objects.filter(is_active=True).order_by("-start_date").first()
        if semester is None:
            raise CommandError("Нет активного семестра.")
        created = generate_lab_sessions(semester=semester, weeks=options["weeks"])
        self.stdout.write(
            self.style.SUCCESS(f"Создано слотов: {created} (семестр «{semester}»).")
        )
