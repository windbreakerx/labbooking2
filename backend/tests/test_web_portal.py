"""Web-каркас и портал привязок (день 5): логин/главная, доступ по ролям,
селектор лаборатории сисадмина, UI привязок.

Чекпоинт дня: завлаб не может привязать чужую дисциплину,
сисадмин может — с обязательной причиной (override).
"""

import pytest
from django.urls import reverse

from apps.academics.models import Department, Discipline, Faculty
from apps.scheduling.models import LabDisciplineBinding, Laboratory, TrainingCenter
from apps.scheduling.views import SESSION_ADMIN_LAB
from apps.users.models import User, UserProfile, UserRole

BINDINGS_URL = "/lab-head/bindings/"
BIND_URL = "/lab-head/bindings/disciplines/{pk}/bind/"
UNBIND_URL = "/lab-head/bindings/disciplines/{pk}/unbind/"


def _user(email, role, *, laboratory=None, training_center=None):
    user = User.objects.create_user(
        email=email, password="pass", first_name="Т", last_name="Ю", role=role, is_staff=True
    )
    UserProfile.objects.create(user=user, laboratory=laboratory, training_center=training_center)
    return user


@pytest.fixture
def faculty_phys(db):
    return Faculty.objects.create(code="ФИЗ", title="Физический")


@pytest.fixture
def faculty_chem(db):
    return Faculty.objects.create(code="ХИМ", title="Химический")


@pytest.fixture
def tc(db):
    return TrainingCenter.objects.create(number=21, name="УЦ портала")


@pytest.fixture
def lab(faculty_phys, tc):
    return Laboratory.objects.create(
        training_center=tc, name="Лаборатория физики", faculty=faculty_phys
    )


@pytest.fixture
def phys_discipline(db, faculty_phys):
    dept = Department.objects.create(title="Кафедра физики", faculty=faculty_phys)
    return Discipline.objects.create(title="Механика", department=dept, is_published=True)


@pytest.fixture
def chem_discipline(db, faculty_chem):
    dept = Department.objects.create(title="Кафедра химии", faculty=faculty_chem)
    return Discipline.objects.create(title="Органика", department=dept, is_published=True)


@pytest.fixture
def lab_head(lab, tc):
    return _user("labhead@spmi.ru", UserRole.LAB_HEAD, laboratory=lab, training_center=tc)


@pytest.fixture
def sys_admin(db):
    return _user("sysadmin@spmi.ru", UserRole.SYS_ADMIN)


@pytest.fixture
def lab_head_client(client, lab_head):
    client.force_login(lab_head)
    return client


@pytest.fixture
def admin_client_with_lab(client, sys_admin, lab):
    client.force_login(sys_admin)
    session = client.session  # один store на переменную: property каждый раз даёт новый
    session[SESSION_ADMIN_LAB] = lab.pk
    session.save()
    return client


def _page(client, url):
    return client.get(url).content.decode()


@pytest.mark.django_db
class TestLoginSkeleton:
    def test_login_page_renders_for_anonymous(self, client):
        response = client.get(reverse("login"))
        assert response.status_code == 200
        assert "Запись на лабораторные" in _page(client, reverse("login"))

    def test_login_post_success(self, client, lab_head):
        response = client.post(
            reverse("login"), {"username": lab_head.email, "password": "pass"}
        )
        assert response.status_code == 302
        assert response.url == reverse("home")

    def test_login_post_wrong_password(self, client, lab_head):
        response = client.post(
            reverse("login"), {"username": lab_head.email, "password": "wrong"}
        )
        assert response.status_code == 200
        assert "Неверный email или пароль." in response.content.decode()

    def test_authenticated_redirected_from_login(self, client, lab_head):
        client.force_login(lab_head)
        response = client.get(reverse("login"))
        assert response.status_code == 302
        assert response.url == reverse("home")

    def test_home_requires_login(self, client):
        response = client.get(reverse("home"))
        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_logout(self, client, lab_head):
        client.force_login(lab_head)
        response = client.post(reverse("logout"))
        assert response.status_code == 302
        assert response.url == reverse("login")
        assert client.get(reverse("home")).status_code == 302

    def test_home_greeting_by_role(self, client, db):
        student = User.objects.create_user(
            email="s@stud.spmi.ru", password="pass", first_name="Анна", last_name="Иванова"
        )
        client.force_login(student)
        page = _page(client, reverse("home"))
        assert "Здравствуйте, Анна" in page
        assert "Кабинет завлаба" not in page

    def test_home_shows_portal_for_lab_head(self, lab_head_client):
        page = _page(lab_head_client, reverse("home"))
        assert "Кабинет завлаба" in page


@pytest.mark.django_db
class TestPortalAccess:
    def test_bindings_require_login(self, client):
        # Миксин роли гонит анонима на главную, LoginRequiredMixin — на логин.
        response = client.get(BINDINGS_URL, follow=True)
        assert response.status_code == 200
        assert "Запись на лабораторные" in response.content.decode()

    def test_lab_admin_denied(self, client):
        lab_admin = _user("labadmin@spmi.ru", UserRole.LAB_ADMIN)
        client.force_login(lab_admin)
        response = client.get(reverse("lab-head-home"), follow=True)
        assert "Доступ только для заведующего лабораторией." in response.content.decode()

    def test_lab_head_portal_home(self, lab_head_client, lab):
        page = _page(lab_head_client, reverse("lab-head-home"))
        assert lab.name in page
        assert reverse("lab-head-bindings") in page

    def test_lab_head_without_lab_gets_hint(self, client):
        orphan_head = _user("orphan@spmi.ru", UserRole.LAB_HEAD)
        client.force_login(orphan_head)
        response = client.get(BINDINGS_URL, follow=True)
        assert response.status_code == 200
        assert "Сначала выберите лабораторию." in response.content.decode()
        assert "Лаборатория не назначена" in response.content.decode()

    def test_sysadmin_home_without_selection(self, client, sys_admin):
        client.force_login(sys_admin)
        page = _page(client, reverse("lab-head-home"))
        assert "Выберите лабораторию" in page
        assert "name=\"laboratory\"" in page  # селектор в шапке

    def test_sysadmin_bindings_without_lab_redirects(self, client, sys_admin):
        client.force_login(sys_admin)
        response = client.get(BINDINGS_URL, follow=True)
        assert "Сначала выберите лабораторию." in response.content.decode()

    def test_lab_head_header_has_no_lab_selector(self, lab_head_client):
        page = _page(lab_head_client, reverse("home"))
        assert "lab-selector" not in page


@pytest.mark.django_db
class TestLabSelector:
    def test_select_lab_sets_session_and_returns_back(self, client, sys_admin, lab):
        client.force_login(sys_admin)
        response = client.post(
            reverse("lab-head-select-lab"),
            {"laboratory": lab.pk, "next": BINDINGS_URL},
        )
        assert response.status_code == 302
        assert response.url == BINDINGS_URL
        assert client.session[SESSION_ADMIN_LAB] == lab.pk

    def test_select_lab_rejects_external_next(self, client, sys_admin, lab):
        client.force_login(sys_admin)
        response = client.post(
            reverse("lab-head-select-lab"),
            {"laboratory": lab.pk, "next": "https://evil.example/steal"},
        )
        assert response.status_code == 302
        assert response.url == reverse("lab-head-home")

    def test_select_lab_denied_for_lab_head(self, client, lab_head, lab):
        client.force_login(lab_head)
        response = client.post(reverse("lab-head-select-lab"), {"laboratory": lab.pk})
        assert response.status_code == 302
        assert response.url == reverse("home")


@pytest.mark.django_db
class TestBindingsUI:
    """Чекпоинт Дня 5: факультетское правило и override в web-слое."""

    def test_lab_head_sees_only_own_faculty(self, lab_head_client, phys_discipline, chem_discipline):
        page = _page(lab_head_client, BINDINGS_URL)
        assert phys_discipline.title in page
        assert chem_discipline.title not in page

    def test_lab_head_binds_own_faculty(self, lab_head_client, lab_head, lab, phys_discipline):
        response = lab_head_client.post(BIND_URL.format(pk=phys_discipline.pk))
        assert response.status_code == 302
        binding = LabDisciplineBinding.objects.get(laboratory=lab, discipline=phys_discipline)
        assert binding.is_override is False
        assert binding.bound_by == lab_head
        assert response.url == BINDINGS_URL
        assert "привязана" in _page(lab_head_client, BINDINGS_URL)

    def test_lab_head_cannot_bind_mismatch(self, lab_head_client, lab, chem_discipline):
        """Завлаб не может привязать дисциплину чужого факультета (поддельный POST)."""
        response = lab_head_client.post(BIND_URL.format(pk=chem_discipline.pk), follow=True)
        assert "другому факультету" in response.content.decode()
        assert not LabDisciplineBinding.objects.exists()

    def test_sysadmin_sees_all_disciplines_with_override_ui(
        self, admin_client_with_lab, phys_discipline, chem_discipline
    ):
        page = _page(admin_client_with_lab, BINDINGS_URL)
        assert phys_discipline.title in page
        assert chem_discipline.title in page
        assert 'name="override"' in page  # чекбокс у чужой дисциплины

    def test_sysadmin_override_without_reason_denied(
        self, admin_client_with_lab, lab, chem_discipline
    ):
        response = admin_client_with_lab.post(
            BIND_URL.format(pk=chem_discipline.pk), {"override": "on"}, follow=True
        )
        assert "укажите причину" in response.content.decode()
        assert not LabDisciplineBinding.objects.exists()

    def test_sysadmin_override_with_reason(self, admin_client_with_lab, lab, chem_discipline):
        """Сисадмин может привязать чужую дисциплину с причиной."""
        response = admin_client_with_lab.post(
            BIND_URL.format(pk=chem_discipline.pk),
            {"override": "on", "reason": "Межкафедральная лаборатория"},
        )
        assert response.status_code == 302
        binding = LabDisciplineBinding.objects.get(laboratory=lab, discipline=chem_discipline)
        assert binding.is_override is True
        assert binding.reason == "Межкафедральная лаборатория"

    def test_sysadmin_binds_matching_without_override(
        self, admin_client_with_lab, lab, phys_discipline
    ):
        response = admin_client_with_lab.post(BIND_URL.format(pk=phys_discipline.pk))
        assert response.status_code == 302
        binding = LabDisciplineBinding.objects.get(laboratory=lab, discipline=phys_discipline)
        assert binding.is_override is False

    def test_override_audit_shown_on_page(self, admin_client_with_lab, lab, chem_discipline, sys_admin):
        admin_client_with_lab.post(
            BIND_URL.format(pk=chem_discipline.pk),
            {"override": "on", "reason": "Межкафедральная лаборатория"},
        )
        page = _page(admin_client_with_lab, BINDINGS_URL)
        assert "несоответствующая" in page
        assert "Межкафедральная лаборатория" in page
        assert "Ю Т." in page  # bound_by — header_name сисадмина

    def test_duplicate_bind_shows_error(self, lab_head_client, phys_discipline):
        lab_head_client.post(BIND_URL.format(pk=phys_discipline.pk))
        response = lab_head_client.post(BIND_URL.format(pk=phys_discipline.pk), follow=True)
        assert "уже привязана" in response.content.decode()
        assert LabDisciplineBinding.objects.count() == 1

    def test_unbind(self, lab_head_client, phys_discipline):
        lab_head_client.post(BIND_URL.format(pk=phys_discipline.pk))
        response = lab_head_client.post(UNBIND_URL.format(pk=phys_discipline.pk), follow=True)
        assert "отвязана" in response.content.decode()
        assert not LabDisciplineBinding.objects.exists()

    def test_search_filters_candidates(self, lab_head_client, phys_discipline):
        Discipline.objects.create(title="Термодинамика")
        page = _page(lab_head_client, BINDINGS_URL + "?q=механ")
        assert phys_discipline.title in page
        assert "Термодинамика" not in page

    def test_bound_discipline_not_in_candidates(self, lab_head_client, phys_discipline):
        lab_head_client.post(BIND_URL.format(pk=phys_discipline.pk))
        page = _page(lab_head_client, BINDINGS_URL)
        assert BIND_URL.format(pk=phys_discipline.pk) not in page  # нет формы «Привязать»
        assert UNBIND_URL.format(pk=phys_discipline.pk) in page

