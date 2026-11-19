"""Убрать доступ в Django-админку у всех, кроме сисадмина.

`is_staff` — это флаг доступа в `/admin/`, а не бизнес-роль. Импорт и seed
раньше выставляли его заведующим и сотрудникам лабораторий; команда приводит
существующие аккаунты к политике «админка только у SYS_ADMIN».

По умолчанию только показывает расхождения. Суперпользователей не трогает.
"""

from django.core.management.base import BaseCommand

from apps.users.models import User, UserRole


class Command(BaseCommand):
    help = "Снять is_staff у ролей, которым не нужна Django-админка (по умолчанию dry-run)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать изменения (без флага — только отчёт).",
        )

    def handle(self, *args, **options):
        users = (
            User.objects.filter(is_staff=True, is_superuser=False)
            .exclude(role=UserRole.SYS_ADMIN)
            .order_by("email")
        )
        rows = list(users)
        if not rows:
            self.stdout.write("Лишних доступов в админку нет.")
            return

        for user in rows:
            self.stdout.write(f"{user.email} — {user.get_role_display()}")

        if options["apply"]:
            updated = User.objects.filter(pk__in=[user.pk for user in rows]).update(
                is_staff=False
            )
            self.stdout.write(
                self.style.SUCCESS(f"Снят is_staff у {updated} пользователей.")
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Найдено {len(rows)} пользователей с лишним is_staff. "
                    "Запустите с --apply, чтобы снять."
                )
            )
