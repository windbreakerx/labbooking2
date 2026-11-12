"""Демо-инфраструктура пилота (день 7): факультет, УЦ, лаборатория, аудитории, стафф.

Данные (группы, дисциплины, студенты) приходят через ``import_dekanat_csv``,
слоты — через редактор расписания и ``generate_sessions``. Идемпотентно:
повторный прогон обновляет существующее, дубликатов не создаёт; пароли
сотрудников задаются только при создании учётной записи.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.academics.models import Department, Faculty, Semester
from apps.scheduling.models import Laboratory, LaboratoryType, Room, TrainingCenter
from apps.users.models import User, UserProfile, UserRole

LAB_NAME = "Комплексная учебная лаборатория нефтегазового факультета"
SEMESTER_NAME = "Пилот 2026/2027 (нефтегаз)"
PILOT_PASSWORD = "pilot123"
DEPARTMENTS = [
    "Кафедра транспорта и хранения нефти и газа",
    "Кафедра бурения скважин",
    "Кафедра разработки и эксплуатации месторождений",
]
ROOMS = [
    ("1123", 10), ("1124", 15), ("2105", 21), ("2111", 15), ("2112", 20),
    ("2113", 15), ("2114", 5), ("2115", 21), ("2116", 16), ("2117", 15),
    ("2118", 5), ("3314", 14), ("3316", 15), ("3420", 24), ("3428", 44),
]
STAFF = [
    ("zavlab.pilot@spmi.ru", "Павел", "Логинов", UserRole.LAB_HEAD),
    ("operator1.pilot@spmi.ru", "Ольга", "Егорова", UserRole.LAB_ADMIN),
    ("operator2.pilot@spmi.ru", "Дмитрий", "Белов", UserRole.LAB_ADMIN),
]


class Command(BaseCommand):
    help = "Инфраструктура пилота: факультет НГФ, УЦ №1, лаборатория, 15 аудиторий, завлаб и два сотрудника, активный семестр."

    def handle(self, *args, **options):
        faculty = Faculty.objects.update_or_create(
            code="НГФ", defaults={"title": "Нефтегазовый факультет", "ordering": 0}
        )[0]
        for title in DEPARTMENTS:
            Department.objects.update_or_create(title=title, defaults={"faculty": faculty})
        training_center = TrainingCenter.objects.update_or_create(
            number=1, defaults={"name": LAB_NAME}
        )[0]
        laboratory = Laboratory.objects.update_or_create(
            training_center=training_center,
            name=LAB_NAME,
            defaults={"faculty": faculty, "lab_type": LaboratoryType.COMPLEX},
        )[0]
        for number, capacity in ROOMS:
            Room.objects.update_or_create(
                training_center=training_center,
                number=number,
                defaults={"capacity": capacity, "laboratory": laboratory},
            )

        today = timezone.localdate()
        Semester.objects.exclude(name=SEMESTER_NAME).update(is_active=False)
        Semester.objects.update_or_create(
            name=SEMESTER_NAME,
            defaults={
                "start_date": today - timezone.timedelta(days=7),
                "end_date": today + timezone.timedelta(days=140),
                "is_active": True,
            },
        )

        for email, first_name, last_name, role in STAFF:
            user, created = User.objects.update_or_create(
                email=email,
                defaults={
                    "first_name": first_name,
                    "last_name": last_name,
                    "role": role,
                    "is_staff": True,
                },
            )
            if created:
                user.set_password(PILOT_PASSWORD)
                user.save(update_fields=["password"])
            UserProfile.objects.update_or_create(
                user=user,
                defaults={"training_center": training_center, "laboratory": laboratory},
            )

        self.stdout.write(self.style.SUCCESS("Инфраструктура пилота готова (идемпотентно)."))
        self.stdout.write(f"Завлаб: zavlab.pilot@spmi.ru / {PILOT_PASSWORD}")
        self.stdout.write(f"Сотрудники: operator1.pilot@spmi.ru, operator2.pilot@spmi.ru / {PILOT_PASSWORD}")
        self.stdout.write(
            "Дальше: import_dekanat_csv (groups, disciplines, lab_bindings, curriculum, staff, teachers, students), "
            "затем расписание в интерфейсе завлаба и generate_sessions."
        )
