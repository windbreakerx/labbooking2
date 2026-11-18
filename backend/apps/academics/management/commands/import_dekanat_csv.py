"""Импорт пилотных данных из CSV (день 7, заглушка до живого API Деканата).

Типы: groups, disciplines, lab_bindings, curriculum, staff, teachers, students.
Дедуп студентов: dekanat_id → student_id → email (роль STUDENT); email
существующего аккаунта мигрируется на новый из CSV, если свободен. Пароли
существующих пользователей не меняются без ``--reset-passwords``; роль и
``is_staff`` существующего аккаунта не меняются — такая строка пропускается
с причиной. Каждая строка пишется в отдельной транзакции, ошибки выводятся
с номером строки, полем и причиной. Привязки дисциплин к лабораториям —
через LabDisciplineBinding (аудит), у ЛР — M2M.
"""

import csv
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.academics.models import (
    Department,
    Discipline,
    GroupDisciplineLoad,
    GroupDisciplineLoadSource,
    LabWork,
    Semester,
    StudentGroup,
)
from apps.scheduling.models import LabDisciplineBinding, TrainingCenter
from apps.users.models import User, UserProfile, UserRole

IMPORT_TYPES = ("students", "teachers", "staff", "disciplines", "groups", "curriculum", "lab_bindings")
STAFF_ROLES = {UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.SYS_ADMIN}
PILOT_DEPARTMENT_CODE_RANGES = (
    ("Кафедра транспорта и хранения нефти и газа", 1, 11),
    ("Кафедра бурения скважин", 12, 24),
    ("Кафедра разработки и эксплуатации месторождений", 25, 31),
)


class Command(BaseCommand):
    help = "Импорт данных пилота из CSV (заглушка до API Деканата)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--type", choices=IMPORT_TYPES, required=True)
        parser.add_argument("--encoding", default="utf-8-sig", help="Кодировка CSV (по умолчанию utf-8-sig)")
        parser.add_argument("--delimiter", default=",", help="Разделитель CSV (по умолчанию запятая)")
        parser.add_argument("--default-password", default="changeme123", help="Пароль, если в CSV он не передан")
        parser.add_argument("--semester", default="Пилот 2026/2027 (нефтегаз)", help="Семестр для дисциплин/нагрузки")
        parser.add_argument("--generated-domain", default="demo.local", help="Домен для авто-сгенерированных email")
        parser.add_argument("--import-batch-id", default="", help="Идентификатор пакета импорта (трассировка)")
        parser.add_argument("--only-groups", default="", help="Группы через запятую для частичного импорта")
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Сбросить пароли существующих пользователей (по умолчанию пароль не меняется)",
        )

    # --- утилиты -------------------------------------------------------------

    def _skip(self, field: str, reason: str):
        self.last_skip_reason = f"{field}: {reason}" if field else reason
        return 0, 0

    def _password_from_row(self, row) -> str:
        return self._clean(row, "password", self.default_password) or self.default_password

    @staticmethod
    def _as_bool(value, default=False):
        if value is None:
            return default
        normalized = str(value).strip().lower()
        return default if not normalized else normalized in {"1", "true", "yes", "y", "да"}

    @staticmethod
    def _clean(row, key, default=""):
        return str(row.get(key, default) or default).strip()

    def _sanitize_email_local(self, raw: str) -> str:
        candidate = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        return candidate or "user"

    def _next_generated_email(self, prefix: str) -> str:
        local = self._sanitize_email_local(prefix)
        candidate = f"{local}@{self.generated_domain}"
        suffix = 1
        while User.objects.filter(email=candidate).exists():
            suffix += 1
            candidate = f"{local}-{suffix}@{self.generated_domain}"
        return candidate

    @staticmethod
    def _split_codes(value: str) -> list[str]:
        if not value:
            return []
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]

    def _resolve_discipline(self, *, code="", title=""):
        code, title = self._clean({"code": code}, "code"), self._clean({"title": title}, "title")
        if code:
            found = Discipline.objects.filter(code=code).first()
            if found:
                return found
        return Discipline.objects.filter(title=title).first() if title else None

    def _resolve_semester(self, semester_name: str = "") -> Semester | None:
        if semester_name and (found := Semester.objects.filter(name=semester_name).first()):
            return found
        return Semester.objects.filter(is_active=True).first()

    def _resolve_training_center(self, number: str):
        if number.isdigit():
            return TrainingCenter.objects.filter(number=int(number)).first()
        return None

    def _bind_laboratory(self, discipline: Discipline, training_center) -> bool:
        """Привязка дисциплины к лаборатории УЦ (v2: LabDisciplineBinding, не M2M УЦ)."""
        laboratory = training_center.laboratories.first() if training_center else None
        if laboratory is None:
            return False
        _, created = LabDisciplineBinding.objects.get_or_create(laboratory=laboratory, discipline=discipline)
        return created

    def _resolve_email(self, row, *, role):
        explicit = self._clean(row, "email").lower()
        if explicit:
            return explicit
        if role == UserRole.STUDENT:
            hint = self._clean(row, "student_id") or self._clean(row, "dekanat_id") or self._clean(row, "group")
            return self._next_generated_email(f"student-{hint}")
        hint = self._clean(row, "last_name") or self._clean(row, "dekanat_id")
        return self._next_generated_email(f"{'teacher' if role == UserRole.TEACHER else 'staff'}-{hint}")

    def _compose_first_name(self, row) -> str:
        first_name, patronymic = self._clean(row, "first_name"), self._clean(row, "patronymic")
        if patronymic and patronymic not in first_name:
            return f"{first_name} {patronymic}".strip()
        return first_name

    def _upsert_user(self, row, *, role, is_staff, existing_user: User | None = None):
        email = self._resolve_email(row, role=role).lower()
        first_name = self._compose_first_name(row)
        last_name = self._clean(row, "last_name")
        user = existing_user or User.objects.filter(email=email).first()

        if user is not None:
            if user.role != role:
                self._skip(
                    "role",
                    f"у пользователя {user.email} роль {user.role}, в CSV {role}; "
                    "роль и is_staff не меняются",
                )
                return None, False
            if user.email != email and not User.objects.filter(email=email).exclude(pk=user.pk).exists():
                user.email = email
            user.first_name = first_name
            user.last_name = last_name
            if self.reset_passwords:
                user.set_password(self._password_from_row(row))
            user.save()
            return user, False

        user = User.objects.create_user(
            email=email,
            password=self._password_from_row(row),
            first_name=first_name,
            last_name=last_name,
            role=role,
            is_staff=is_staff,
        )
        return user, True

    def _attach_profile(self, user: User, training_center, *, bind_laboratory: bool):
        profile = UserProfile.objects.get_or_create(user=user)[0]
        if training_center is not None:
            profile.training_center = training_center
            if bind_laboratory and not profile.laboratory_id:
                profile.laboratory = training_center.laboratories.first()
            profile.save(update_fields=["training_center", "laboratory"])
        return profile

    def _bind_profile_disciplines(self, user: User, discipline_codes: str):
        codes = self._split_codes(discipline_codes)
        disciplines = [d for code in codes if (d := self._resolve_discipline(code=code))]
        if disciplines:
            UserProfile.objects.get_or_create(user=user)[0].disciplines.add(*disciplines)

    # --- импортёры типов -----------------------------------------------------

    def _import_group(self, row):
        name = self._clean(row, "name")
        if not name:
            return self._skip("name", "не заполнено")
        if self.only_groups and name not in self.only_groups:
            return self._skip("name", f"группа «{name}» не входит в --only-groups")
        _, created = StudentGroup.objects.update_or_create(
            name=name,
            defaults={"faculty": self._clean(row, "faculty"), "dekanat_id": self._clean(row, "dekanat_id")},
        )
        return int(created), int(not created)

    def _resolve_department(self, code: str):
        match = re.fullmatch(r"NGF-(\d{1,3})", code or "")
        if not match:
            return None
        number = int(match.group(1))
        for title, low, high in PILOT_DEPARTMENT_CODE_RANGES:
            if low <= number <= high:
                return Department.objects.filter(title=title).first()
        return None

    def _import_discipline(self, row):
        title = self._clean(row, "title")
        if not title:
            return self._skip("title", "не заполнено")
        code = self._clean(row, "code")
        semester = self._resolve_semester(self.semester_name)
        defaults = {
            "title": title,
            "semester": semester,
            "dekanat_id": self._clean(row, "dekanat_id"),
            "description": self._clean(row, "description"),
            "is_published": self._as_bool(row.get("is_published"), default=True),
        }
        department = self._resolve_department(code)
        if department:
            defaults["department"] = department
        if code:
            discipline, created = Discipline.objects.update_or_create(code=code, defaults=defaults)
        else:
            discipline, created = Discipline.objects.update_or_create(title=title, defaults=defaults)
        if not discipline.code:
            discipline.code = f"DISC-{discipline.id}"
            discipline.save(update_fields=["code"])
        self._bind_laboratory(discipline, self._resolve_training_center(self._clean(row, "training_center_number")))
        return int(created), int(not created)

    def _import_curriculum(self, row):
        group_name = self._clean(row, "group_name") or self._clean(row, "group")
        discipline_code = self._clean(row, "discipline_code") or self._clean(row, "code")
        if not group_name:
            return self._skip("group_name", "не заполнено")
        if not discipline_code:
            return self._skip("discipline_code", "не заполнено")
        if self.only_groups and group_name not in self.only_groups:
            return self._skip("group_name", f"группа «{group_name}» не входит в --only-groups")
        semester = self._resolve_semester(self._clean(row, "semester") or self.semester_name)
        group = StudentGroup.objects.filter(name=group_name).first()
        discipline = self._resolve_discipline(code=discipline_code)
        if semester is None:
            return self._skip("semester", "семестр не найден")
        if group is None:
            return self._skip("group_name", f"группа «{group_name}» не найдена")
        if discipline is None:
            return self._skip("discipline_code", f"дисциплина «{discipline_code}» не найдена")
        _, created = GroupDisciplineLoad.objects.update_or_create(
            group=group,
            semester=semester,
            discipline=discipline,
            defaults={
                "source": GroupDisciplineLoadSource.IMPORTED_WORKLOAD,
                "is_active": True,
                "import_batch_id": self.import_batch_id,
            },
        )
        group.disciplines.add(discipline)
        return int(created), int(not created)

    def _import_lab_binding(self, row):
        discipline_code = self._clean(row, "discipline_code")
        training_center = self._resolve_training_center(self._clean(row, "training_center_number"))
        discipline = self._resolve_discipline(code=discipline_code)
        laboratory = training_center.laboratories.first() if training_center else None
        if not discipline_code:
            return self._skip("discipline_code", "не заполнено")
        if training_center is None:
            return self._skip("training_center_number", "учебный центр не найден")
        if laboratory is None:
            return self._skip("training_center_number", "у учебного центра нет лаборатории")
        if discipline is None:
            return self._skip("discipline_code", f"дисциплина «{discipline_code}» не найдена")
        created = self._bind_laboratory(discipline, training_center)
        lab_work_number = self._clean(row, "lab_work_number")
        if lab_work_number.isdigit():
            lab_work = LabWork.objects.filter(disciplines=discipline, number=int(lab_work_number)).first()
            if lab_work is None:
                return self._skip("lab_work_number", f"ЛР №{lab_work_number} не найдена у дисциплины")
            lab_work.laboratories.add(laboratory)
            lab_work.training_centers.add(training_center)
            return 0, 1
        for lab_work in LabWork.objects.filter(disciplines=discipline):
            lab_work.laboratories.add(laboratory)
            lab_work.training_centers.add(training_center)
        return int(created), int(not created)

    def _import_teacher(self, row):
        user, created = self._upsert_user(
            row, role=UserRole.TEACHER, is_staff=self._as_bool(row.get("is_staff"), default=False)
        )
        if user is None:
            return 0, 0
        self._attach_profile(
            user, self._resolve_training_center(self._clean(row, "training_center_number")), bind_laboratory=False
        )
        self._bind_profile_disciplines(user, self._clean(row, "discipline_codes"))
        return int(created), int(not created)

    def _import_staff(self, row):
        role = self._clean(row, "role", UserRole.LAB_ADMIN)
        if role not in STAFF_ROLES:
            role = UserRole.LAB_ADMIN
        user, created = self._upsert_user(
            row, role=role, is_staff=self._as_bool(row.get("is_staff"), default=True)
        )
        if user is None:
            return 0, 0
        self._attach_profile(
            user, self._resolve_training_center(self._clean(row, "training_center_number")), bind_laboratory=True
        )
        self._bind_profile_disciplines(user, self._clean(row, "discipline_codes"))
        return int(created), int(not created)

    def _import_student(self, row):
        group_name = self._clean(row, "group")
        if self.only_groups and group_name not in self.only_groups:
            return self._skip(
                "group", f"группа «{group_name or '—'}» не входит в --only-groups"
            )
        dekanat_id, student_id = self._clean(row, "dekanat_id"), self._clean(row, "student_id")
        explicit_email = self._clean(row, "email").lower()

        existing_user = None
        profiles = UserProfile.objects.select_related("user").filter(user__role=UserRole.STUDENT)
        if dekanat_id and (profile := profiles.filter(dekanat_id=dekanat_id).first()):
            existing_user = profile.user
        if existing_user is None and student_id and (profile := profiles.filter(student_id=student_id).first()):
            existing_user = profile.user
        if existing_user is None and explicit_email:
            existing_user = User.objects.filter(email=explicit_email, role=UserRole.STUDENT).first()

        user, created = self._upsert_user(row, role=UserRole.STUDENT, is_staff=False, existing_user=existing_user)
        if user is None:
            return 0, 0
        defaults = {
            "group_name": group_name,
            "dekanat_id": dekanat_id,
            "student_id": student_id,
            "faculty": self._clean(row, "faculty"),
            "gender": self._clean(row, "gender"),
            "phone": self._clean(row, "phone"),
        }
        if group_name and (student_group := StudentGroup.objects.filter(name=group_name).first()):
            defaults["student_group"] = student_group
        UserProfile.objects.update_or_create(user=user, defaults=defaults)
        return int(created), int(not created)

    # --- запуск --------------------------------------------------------------

    def handle(self, *args, **options):
        self.default_password = options["default_password"]
        self.generated_domain = options["generated_domain"]
        self.import_batch_id = options["import_batch_id"]
        self.semester_name = options["semester"]
        self.only_groups = {part for part in options["only_groups"].split(",") if part.strip()}
        self.reset_passwords = options["reset_passwords"]
        self.last_skip_reason = ""

        handlers = {
            "students": self._import_student,
            "teachers": self._import_teacher,
            "staff": self._import_staff,
            "disciplines": self._import_discipline,
            "groups": self._import_group,
            "curriculum": self._import_curriculum,
            "lab_bindings": self._import_lab_binding,
        }
        counts = {"created": 0, "updated": 0, "skipped": 0}
        with open(options["csv_path"], newline="", encoding=options["encoding"]) as csv_file:
            reader = csv.DictReader(csv_file, delimiter=options["delimiter"])
            for line_number, row in enumerate(reader, start=2):
                self.last_skip_reason = ""
                try:
                    with transaction.atomic():
                        created, updated = handlers[options["type"]](row)
                except Exception as exc:  # строка не должна ронять весь файл
                    counts["skipped"] += 1
                    self.stdout.write(
                        self.style.ERROR(f"Строка {line_number}: ошибка — {exc}")
                    )
                    continue
                if created or updated:
                    counts["created" if created else "updated"] += 1
                    continue
                counts["skipped"] += 1
                reason = self.last_skip_reason or "строка не дала данных для импорта"
                self.stdout.write(
                    self.style.WARNING(f"Строка {line_number}: пропущена — {reason}")
                )
        self.stdout.write(self.style.SUCCESS("Импорт завершён."))
        self.stdout.write(f"Создано: {counts['created']}")
        self.stdout.write(f"Обновлено: {counts['updated']}")
        self.stdout.write(f"Пропущено: {counts['skipped']}")
