"""BindingService: факультетское правило и override для SYS_ADMIN (флагман Дня 4–5)."""

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.academics.models import Department, Discipline, Faculty, Semester
from apps.scheduling.models import LabDisciplineBinding, Laboratory, TrainingCenter
from apps.scheduling.services import BindingError, bind, bindable_disciplines_qs, can_bind, unbind
from apps.users.models import User, UserProfile, UserRole


def _user(email, role, *, laboratory=None, training_center=None):
    user = User.objects.create_user(
        email=email, password="pass", first_name="T", last_name="U", role=role, is_staff=True
    )
    UserProfile.objects.create(user=user, laboratory=laboratory, training_center=training_center)
    return user


@pytest.fixture
def semester(db):
    return Semester.objects.create(
        name="Binding", start_date="2026-01-01", end_date="2026-12-31", is_active=True
    )


@pytest.fixture
def faculty_phys(db):
    return Faculty.objects.create(code="ФИЗ", title="Физический")


@pytest.fixture
def faculty_chem(db):
    return Faculty.objects.create(code="ХИМ", title="Химический")


@pytest.fixture
def dept_phys(faculty_phys):
    return Department.objects.create(title="Кафедра физики", faculty=faculty_phys)


@pytest.fixture
def dept_chem(faculty_chem):
    return Department.objects.create(title="Кафедра химии", faculty=faculty_chem)


@pytest.fixture
def tc(db):
    return TrainingCenter.objects.create(number=21, name="УЦ привязок")


@pytest.fixture
def lab(faculty_phys, tc):
    return Laboratory.objects.create(
        training_center=tc, name="Лаборатория физики", faculty=faculty_phys
    )


@pytest.fixture
def phys_discipline(semester, dept_phys):
    return Discipline.objects.create(
        title="Механика", semester=semester, department=dept_phys, is_published=True
    )


@pytest.fixture
def chem_discipline(semester, dept_chem):
    return Discipline.objects.create(
        title="Органика", semester=semester, department=dept_chem, is_published=True
    )


@pytest.fixture
def lab_head(lab, tc):
    return _user("labhead@spmi.ru", UserRole.LAB_HEAD, laboratory=lab, training_center=tc)


@pytest.fixture
def sys_admin(db):
    return _user("sysadmin@spmi.ru", UserRole.SYS_ADMIN)


@pytest.mark.django_db
class TestCanBind:
    def test_matrix(self, lab_head, sys_admin, lab, phys_discipline, chem_discipline):
        assert can_bind(lab_head, lab, phys_discipline) is True
        assert can_bind(lab_head, lab, chem_discipline) is False
        assert can_bind(sys_admin, lab, chem_discipline) is True
        teacher = _user("teacher@spmi.ru", UserRole.TEACHER)
        lab_admin = _user("labadmin@spmi.ru", UserRole.LAB_ADMIN)
        student = _user("s@stud.spmi.ru", UserRole.STUDENT)
        for user in (teacher, lab_admin, student):
            assert can_bind(user, lab, phys_discipline) is False
        assert can_bind(AnonymousUser(), lab, phys_discipline) is False

    def test_lab_without_faculty_is_never_own(self, lab_head, tc, phys_discipline):
        no_faculty_lab = Laboratory.objects.create(training_center=tc, name="Без факультета")
        assert can_bind(lab_head, no_faculty_lab, phys_discipline) is False


@pytest.mark.django_db
class TestBind:
    def test_lab_head_binds_own_faculty(self, lab_head, lab, phys_discipline):
        binding = bind(lab_head, lab, phys_discipline)
        assert binding.is_override is False
        assert binding.bound_by == lab_head
        assert phys_discipline.laboratories.filter(pk=lab.pk).exists()

    def test_lab_head_cannot_bind_mismatch(self, lab_head, lab, chem_discipline):
        with pytest.raises(BindingError, match="другому факультету"):
            bind(lab_head, lab, chem_discipline)

    def test_lab_head_cannot_override(self, lab_head, lab, chem_discipline):
        with pytest.raises(BindingError, match="только системный администратор"):
            bind(lab_head, lab, chem_discipline, override=True, reason="Очень нужно")

    def test_lab_admin_cannot_bind(self, lab, phys_discipline):
        lab_admin = _user("labadmin2@spmi.ru", UserRole.LAB_ADMIN)
        with pytest.raises(BindingError, match="только заведующему"):
            bind(lab_admin, lab, phys_discipline)

    def test_sysadmin_override_requires_reason(self, sys_admin, lab, chem_discipline):
        with pytest.raises(BindingError, match="причину"):
            bind(sys_admin, lab, chem_discipline, override=True)

    def test_sysadmin_override_with_reason(self, sys_admin, lab, chem_discipline):
        binding = bind(
            sys_admin, lab, chem_discipline, override=True, reason="Межкафедральная лаборатория"
        )
        assert binding.is_override is True
        assert binding.reason == "Межкафедральная лаборатория"
        assert binding.bound_by == sys_admin

    def test_sysadmin_mismatch_without_override_denied(self, sys_admin, lab, chem_discipline):
        with pytest.raises(BindingError, match="другому факультету"):
            bind(sys_admin, lab, chem_discipline)

    def test_duplicate_rejected(self, lab_head, lab, phys_discipline):
        bind(lab_head, lab, phys_discipline)
        with pytest.raises(BindingError, match="уже привязана"):
            bind(lab_head, lab, phys_discipline)

    def test_unbind(self, lab_head, lab, phys_discipline):
        bind(lab_head, lab, phys_discipline)
        unbind(lab_head, lab, phys_discipline)
        assert not LabDisciplineBinding.objects.filter(
            laboratory=lab, discipline=phys_discipline
        ).exists()
        with pytest.raises(BindingError, match="не найдена"):
            unbind(lab_head, lab, phys_discipline)

    def test_unbind_denied_for_lab_admin(self, lab, phys_discipline):
        lab_admin = _user("labadmin3@spmi.ru", UserRole.LAB_ADMIN)
        with pytest.raises(BindingError, match="только заведующему"):
            unbind(lab_admin, lab, phys_discipline)


@pytest.mark.django_db
class TestBindableDisciplines:
    def test_lab_head_sees_own_faculty_only(self, lab_head, lab, phys_discipline, chem_discipline):
        ids = set(bindable_disciplines_qs(lab_head, lab).values_list("pk", flat=True))
        assert phys_discipline.pk in ids
        assert chem_discipline.pk not in ids

    def test_sys_admin_sees_all(self, sys_admin, lab, phys_discipline, chem_discipline):
        ids = set(bindable_disciplines_qs(sys_admin, lab).values_list("pk", flat=True))
        assert phys_discipline.pk in ids
        assert chem_discipline.pk in ids
