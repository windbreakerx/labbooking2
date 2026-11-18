"""Сверка счётчиков неявок с фактическими записями.

По умолчанию команда только показывает расхождения. Запись исправлений —
через ``--apply``. Флаг ``explanation_required`` не трогаем: его снимает
сотрудник вручную, счётчик при этом сохраняется.
"""

from collections import Counter

from django.core.management.base import BaseCommand

from apps.bookings.models import Booking, StudentLabAttendance
from apps.bookings.services.attendance import resolve_booking_laboratory


class Command(BaseCommand):
    help = "Пересчитать StudentLabAttendance.no_show_count из записей с had_no_show"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать исправления. Без флага команда только показывает расхождения.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        counts: Counter[tuple[int, int]] = Counter()
        for booking in Booking.objects.filter(had_no_show=True).select_related(
            "room__laboratory", "lab_work", "discipline"
        ):
            laboratory = resolve_booking_laboratory(booking)
            if laboratory is not None:
                counts[(booking.student_id, laboratory.pk)] += 1

        attendance_rows = list(
            StudentLabAttendance.objects.select_related("student", "laboratory")
        )
        existing = {(row.student_id, row.laboratory_id) for row in attendance_rows}

        mismatches = 0
        for row in attendance_rows:
            expected = counts.get((row.student_id, row.laboratory_id), 0)
            if row.no_show_count == expected:
                continue
            mismatches += 1
            self.stdout.write(
                f"{row.student.email} @ {row.laboratory}: "
                f"было {row.no_show_count}, должно быть {expected}"
            )
            if apply_changes:
                row.no_show_count = expected
                row.save(update_fields=["no_show_count", "updated_at"])

        for (student_id, laboratory_id), expected in counts.items():
            if (student_id, laboratory_id) in existing:
                continue
            mismatches += 1
            self.stdout.write(
                f"Нет строки посещаемости: student_id={student_id}, "
                f"laboratory_id={laboratory_id}, неявок {expected}"
            )
            if apply_changes:
                StudentLabAttendance.objects.create(
                    student_id=student_id,
                    laboratory_id=laboratory_id,
                    no_show_count=expected,
                )

        if mismatches == 0:
            self.stdout.write(self.style.SUCCESS("Расхождений нет."))
            return
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"Исправлено расхождений: {mismatches}."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Расхождений: {mismatches}. Для записи запустите команду с --apply."
                )
            )
