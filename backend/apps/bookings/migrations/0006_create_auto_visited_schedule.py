"""Создать ежедневное расписание автопосещения в django-q2."""

from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def create_auto_visited_schedule(apps, schema_editor):
    if not apps.is_installed("django_q"):
        return
    Schedule = apps.get_model("django_q", "Schedule")
    now = timezone.now()
    next_run = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    Schedule.objects.get_or_create(
        func="apps.bookings.tasks.mark_visited_task",
        defaults={
            "name": "Автопосещение завершённых слотов",
            "schedule_type": Schedule.DAILY,
            "next_run": next_run,
            "repeats": -1,
        },
    )


def remove_auto_visited_schedule(apps, schema_editor):
    if not apps.is_installed("django_q"):
        return
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(func="apps.bookings.tasks.mark_visited_task").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0005_alter_booking_registration_type"),
    ]

    operations = [
        migrations.RunPython(
            create_auto_visited_schedule,
            reverse_code=remove_auto_visited_schedule,
        ),
    ]
