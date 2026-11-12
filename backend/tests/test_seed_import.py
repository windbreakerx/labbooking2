"""seed_demo и import_dekanat_csv (день 7): инфраструктура пилота и CSV-импорт.

Проверки: идемпотентность seed и состав инфраструктуры; импорт всех семи
типов, дедуп студентов по dekanat_id с миграцией email, привязки дисциплин
через LabDisciplineBinding, пропуск неразрешимых строк, фильтр --only-groups.
"""

import csv

import pytest
from django.core.management import call_command

from apps.academics.models import (
    Department,
    Discipline,
    Faculty,
    GroupDisciplineLoad,
    LabWork,
    Semester,
    StudentGroup,
)
from apps.scheduling.models import (
    LabDisciplineBinding,
    Laboratory,
    LaboratoryType,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserRole

SEMESTER_NAME = "Пилот 2026/2027 (нефтегаз)"


def _write_csv(tmp_path, name, fieldnames, rows):
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _run_import(path, import_type, **kwargs):
    call_command("import_dekanat_csv", path, type=import_type, **kwargs)


@pytest.fixture
def seeded(db):
    call_command("seed_demo")
    return TrainingCenter.objects.get(number=1)


@pytest.mark.django_db
class TestSeedDemo:
    def test_creates_infrastructure(self):
        Semester.objects.create(
            name="Прошлый", start_date="2025-09-01", end_date="2026-06-30", is_active=True
        )
        call_command("seed_demo")

        faculty = Faculty.objects.get(code="НГФ")
        training_center = TrainingCenter.objects.get(number=1)
        laboratory = Laboratory.objects.get(training_center=training_center)
        assert laboratory.faculty == faculty
        assert laboratory.lab_type == LaboratoryType.COMPLEX
        assert Department.objects.filter(faculty=faculty).count() == 3

        rooms = Room.objects.filter(training_center=training_center)
        assert rooms.count() == 15
        assert all(room.laboratory == laboratory for room in rooms)
        assert rooms.get(number="1123").capacity == 10
        assert rooms.get(number="3428").capacity == 44

        head = User.objects.get(email="zavlab.pilot@spmi.ru")
        assert head.role == UserRole.LAB_HEAD and head.is_staff
        assert head.profile.laboratory == laboratory
        assert User.objects.get(email="operator1.pilot@spmi.ru").role == UserRole.LAB_ADMIN

        pilot = Semester.objects.get(name=SEMESTER_NAME)
        assert pilot.is_active
        assert not Semester.objects.get(name="Прошлый").is_active

    def test_idempotent(self):
        call_command("seed_demo")
        head_pk = User.objects.get(email="zavlab.pilot@spmi.ru").pk
        room_pk = Room.objects.get(number="1123").pk
        call_command("seed_demo")
        assert User.objects.filter(email__endswith="@spmi.ru").count() == 3
        assert Room.objects.count() == 15
        assert Semester.objects.count() == 1
        assert User.objects.get(email="zavlab.pilot@spmi.ru").pk == head_pk
        assert Room.objects.get(number="1123").pk == room_pk


@pytest.mark.django_db
class TestImportDekanatCsv:
    def test_groups(self, seeded, tmp_path):
        path = _write_csv(
            tmp_path, "groups.csv", ["name", "faculty", "dekanat_id"],
            [{"name": "ТНГ-24", "faculty": "Нефтегазовый", "dekanat_id": "DEK-GR-ТНГ-24"}],
        )
        _run_import(path, "groups")
        group = StudentGroup.objects.get(name="ТНГ-24")
        assert group.dekanat_id == "DEK-GR-ТНГ-24"
        assert group.faculty == "Нефтегазовый"

    def test_disciplines_with_lab_binding(self, seeded, tmp_path):
        path = _write_csv(
            tmp_path, "disciplines.csv",
            ["code", "title", "dekanat_id", "is_published", "description", "training_center_number"],
            [{"code": "NGF-001", "title": "Транспорт углеводородов", "dekanat_id": "DK-D1",
              "is_published": "true", "description": "", "training_center_number": "1"}],
        )
        _run_import(path, "disciplines", import_batch_id="B-1")
        discipline = Discipline.objects.get(code="NGF-001")
        assert discipline.semester.name == SEMESTER_NAME and discipline.is_published
        assert discipline.department.title == "Кафедра транспорта и хранения нефти и газа"
        laboratory = Laboratory.objects.get(training_center=seeded)
        assert LabDisciplineBinding.objects.filter(laboratory=laboratory, discipline=discipline).exists()

        _run_import(path, "disciplines")
        assert Discipline.objects.filter(code="NGF-001").count() == 1

    def test_discipline_department_by_code_range(self, seeded, tmp_path):
        codes = ("NGF-001", "NGF-011", "NGF-012", "NGF-024", "NGF-025", "NGF-031", "ABC-9")
        path = _write_csv(
            tmp_path, "d.csv", ["code", "title", "training_center_number"],
            [{"code": code, "title": f"Дисциплина {code}", "training_center_number": "1"} for code in codes],
        )
        _run_import(path, "disciplines")

        titles = {code: Discipline.objects.get(code=code).department for code in codes}
        tng, bur, dev = (
            "Кафедра транспорта и хранения нефти и газа",
            "Кафедра бурения скважин",
            "Кафедра разработки и эксплуатации месторождений",
        )
        assert titles["NGF-001"].title == tng and titles["NGF-011"].title == tng
        assert titles["NGF-012"].title == bur and titles["NGF-024"].title == bur
        assert titles["NGF-025"].title == dev and titles["NGF-031"].title == dev
        assert titles["ABC-9"] is None

    def test_curriculum_load_and_skip_unknown(self, seeded, tmp_path):
        groups = _write_csv(tmp_path, "g.csv", ["name"], [{"name": "ТНГ-24"}])
        _run_import(groups, "groups")
        disciplines = _write_csv(
            tmp_path, "d.csv", ["code", "title", "training_center_number"],
            [{"code": "NGF-001", "title": "Транспорт углеводородов", "training_center_number": "1"},
             {"code": "NGF-002", "title": "Бурение скважин", "training_center_number": "1"}],
        )
        _run_import(disciplines, "disciplines")
        curriculum = _write_csv(
            tmp_path, "c.csv", ["group_name", "discipline_code"],
            [{"group_name": "ТНГ-24", "discipline_code": "NGF-001"},
             {"group_name": "ТНГ-24", "discipline_code": "MISSING-9"}],
        )
        _run_import(curriculum, "curriculum", import_batch_id="B-2")
        group = StudentGroup.objects.get(name="ТНГ-24")
        load = GroupDisciplineLoad.objects.get(group=group)
        assert load.discipline.code == "NGF-001"
        assert load.import_batch_id == "B-2"
        assert GroupDisciplineLoad.objects.count() == 1
        assert group.disciplines.filter(code="NGF-001").exists()

    def test_students_generated_email_and_group_link(self, seeded, tmp_path):
        groups = _write_csv(tmp_path, "g.csv", ["name"], [{"name": "ТНГ-24"}])
        _run_import(groups, "groups")
        students = _write_csv(
            tmp_path, "s.csv",
            ["email", "password", "last_name", "first_name", "group", "dekanat_id", "student_id"],
            [{"email": "", "password": "", "last_name": "Иванов", "first_name": "Иван",
              "group": "ТНГ-24", "dekanat_id": "DK-S1", "student_id": "ST-2026-0001"}],
        )
        _run_import(students, "students")
        student = User.objects.get(role=UserRole.STUDENT)
        assert student.email == "student-st-2026-0001@demo.local"
        assert student.check_password("changeme123")
        assert student.profile.student_group.name == "ТНГ-24"
        assert student.profile.dekanat_id == "DK-S1"

    def test_student_dedup_by_dekanat_id_migrates_email(self, seeded, tmp_path):
        rows = [{"email": "old@x.local", "password": "p", "last_name": "Старый", "first_name": "С",
                 "group": "", "dekanat_id": "DK-S9", "student_id": ""}]
        fields = ["email", "password", "last_name", "first_name", "group", "dekanat_id", "student_id"]
        _run_import(_write_csv(tmp_path, "s1.csv", fields, rows), "students")
        rows[0].update(email="new@x.local", last_name="Новый")
        _run_import(_write_csv(tmp_path, "s2.csv", fields, rows), "students")

        assert User.objects.filter(role=UserRole.STUDENT).count() == 1
        student = User.objects.get(role=UserRole.STUDENT)
        assert student.email == "new@x.local"
        assert student.last_name == "Новый"

    def test_staff_with_profile_and_disciplines(self, seeded, tmp_path):
        disciplines = _write_csv(
            tmp_path, "d.csv", ["code", "title", "training_center_number"],
            [{"code": "NGF-001", "title": "Транспорт углеводородов", "training_center_number": "1"},
             {"code": "NGF-002", "title": "Бурение скважин", "training_center_number": "1"}],
        )
        _run_import(disciplines, "disciplines")
        staff = _write_csv(
            tmp_path, "staff.csv",
            ["email", "password", "last_name", "first_name", "role", "training_center_number", "is_staff", "discipline_codes"],
            [{"email": "ngflab@spmi.ru", "password": "", "last_name": "Фондаминский", "first_name": "Максим",
              "role": "LAB_HEAD", "training_center_number": "1", "is_staff": "true",
              "discipline_codes": "NGF-001;NGF-002"}],
        )
        _run_import(staff, "staff")
        user = User.objects.get(email="ngflab@spmi.ru")
        assert user.role == UserRole.LAB_HEAD
        assert user.profile.training_center == seeded
        assert user.profile.laboratory == Laboratory.objects.get(training_center=seeded)
        assert set(user.profile.disciplines.values_list("code", flat=True)) == {"NGF-001", "NGF-002"}

    def test_teachers_without_laboratory(self, seeded, tmp_path):
        teachers = _write_csv(
            tmp_path, "t.csv",
            ["email", "password", "last_name", "first_name", "training_center_number", "is_staff", "discipline_codes"],
            [{"email": "teacher@spmi.ru", "password": "", "last_name": "У", "first_name": "Ч",
              "training_center_number": "1", "is_staff": "false", "discipline_codes": ""}],
        )
        _run_import(teachers, "teachers")
        user = User.objects.get(email="teacher@spmi.ru")
        assert user.role == UserRole.TEACHER and not user.is_staff
        assert user.profile.training_center == seeded
        assert user.profile.laboratory is None

    def test_lab_bindings_with_and_without_lab_work(self, seeded, tmp_path):
        disciplines = _write_csv(
            tmp_path, "d.csv", ["code", "title", "training_center_number"],
            [{"code": "NGF-001", "title": "Транспорт углеводородов", "training_center_number": "1"}],
        )
        _run_import(disciplines, "disciplines")
        discipline = Discipline.objects.get(code="NGF-001")
        laboratory = Laboratory.objects.get(training_center=seeded)
        for number in (1, 2):
            work = LabWork.objects.create(number=number, title=f"ЛР {number}", duration_minutes=90)
            work.disciplines.set([discipline])

        bindings = _write_csv(
            tmp_path, "b.csv", ["training_center_number", "discipline_code", "lab_work_number"],
            [{"training_center_number": "1", "discipline_code": "NGF-001", "lab_work_number": "2"}],
        )
        _run_import(bindings, "lab_bindings")
        work2 = LabWork.objects.get(number=2)
        assert work2.laboratories.filter(pk=laboratory.pk).exists()
        assert work2.training_centers.filter(pk=seeded.pk).exists()
        assert not LabWork.objects.get(number=1).laboratories.exists()

        all_works = _write_csv(
            tmp_path, "b2.csv", ["training_center_number", "discipline_code", "lab_work_number"],
            [{"training_center_number": "1", "discipline_code": "NGF-001", "lab_work_number": ""}],
        )
        _run_import(all_works, "lab_bindings")
        assert LabWork.objects.get(number=1).laboratories.filter(pk=laboratory.pk).exists()
        assert LabDisciplineBinding.objects.filter(laboratory=laboratory, discipline=discipline).count() == 1

    def test_only_groups_filter(self, seeded, tmp_path):
        groups = _write_csv(
            tmp_path, "g.csv", ["name"], [{"name": "ТНГ-24"}, {"name": "ЭХТ-24"}]
        )
        _run_import(groups, "groups", only_groups="ТНГ-24")
        assert StudentGroup.objects.count() == 1

        students = _write_csv(
            tmp_path, "s.csv",
            ["email", "password", "last_name", "first_name", "group", "dekanat_id", "student_id"],
            [{"email": "a@x.local", "password": "", "last_name": "А", "first_name": "А",
              "group": "ТНГ-24", "dekanat_id": "DK-1", "student_id": ""},
             {"email": "b@x.local", "password": "", "last_name": "Б", "first_name": "Б",
              "group": "ЭХТ-24", "dekanat_id": "DK-2", "student_id": ""}],
        )
        _run_import(students, "students", only_groups="ТНГ-24")
        assert User.objects.filter(role=UserRole.STUDENT).count() == 1
