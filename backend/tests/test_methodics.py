"""Методички: доступ только авторизованным, загрузка — завлабу.

Проверяем сквозной сценарий: PDF отдаётся через Django (в проде — X-Accel),
студент видит методички своей ЛР, завлаб загружает и удаляет файл, чужая
лаборатория и не-PDF отклоняются.
"""

import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.academics.models import LabWork, LabWorkMethodics
from apps.scheduling.models import Laboratory, TrainingCenter
from apps.users.models import User, UserProfile, UserRole

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def _pdf(name="lr.pdf", content=PDF_BYTES):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def _user(email, role, *, laboratory, training_center):
    user = User.objects.create_user(
        email=email,
        password="pass",  # nosec B106
        first_name="Т",
        last_name="Ю",
        role=role,
        is_staff=True,
    )
    UserProfile.objects.create(
        user=user, laboratory=laboratory, training_center=training_center
    )
    return user


@pytest.fixture(autouse=True)
def _media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def lab_head(room):
    return _user(
        "head@spmi.ru",
        UserRole.LAB_HEAD,
        laboratory=room.laboratory,
        training_center=room.training_center,
    )


@pytest.fixture
def teacher(room):
    return _user(
        "teacher@spmi.ru",
        UserRole.TEACHER,
        laboratory=room.laboratory,
        training_center=room.training_center,
    )


@pytest.fixture
def foreign_lab_work(db):
    tc = TrainingCenter.objects.create(number=9002, name="Чужой УЦ")
    laboratory = Laboratory.objects.create(training_center=tc, name="Чужая лаборатория")
    lab_work = LabWork.objects.create(number=9, title="Чужая ЛР", duration_minutes=90)
    lab_work.laboratories.add(laboratory)
    return lab_work


def _methodic(lab_work, *, name="lr.pdf", title="", uploaded_by=None):
    return LabWorkMethodics.objects.create(
        lab_work=lab_work,
        file=_pdf(name),
        title=title,
        uploaded_by=uploaded_by,
    )


@pytest.mark.django_db
class TestMethodicAccess:
    def test_anonymous_redirected_to_login(self, client, lab_work):
        methodic = _methodic(lab_work)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_student_opens_own_methodic_inline(self, client, student, lab_work):
        methodic = _methodic(lab_work)
        client.force_login(student)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith("inline;")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"".join(response.streaming_content) == PDF_BYTES

    def test_download_forces_attachment(self, client, student, lab_work):
        methodic = _methodic(lab_work)
        client.force_login(student)
        response = client.get(reverse("methodic-file", args=[methodic.pk]) + "?download=1")
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment;")

    def test_student_cannot_open_foreign_lab_work_methodic(
        self, client, student, foreign_lab_work
    ):
        methodic = _methodic(foreign_lab_work)
        client.force_login(student)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 403

    def test_lab_head_opens_own_methodic(self, client, lab_head, lab_work):
        methodic = _methodic(lab_work)
        client.force_login(lab_head)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 200

    def test_lab_head_cannot_open_foreign_methodic(self, client, lab_head, foreign_lab_work):
        methodic = _methodic(foreign_lab_work)
        client.force_login(lab_head)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 403

    def test_prod_response_uses_x_accel_redirect(self, client, student, lab_work, settings):
        methodic = _methodic(lab_work, name="лр 1.pdf")
        settings.USE_X_ACCEL_REDIRECT = True
        client.force_login(student)
        response = client.get(reverse("methodic-file", args=[methodic.pk]))
        assert response.status_code == 200
        assert response["X-Accel-Redirect"].startswith("/protected-media/methodics/")
        assert "%D0" in response["X-Accel-Redirect"]  # кириллица закодирована
        assert response.content == b""

    def test_student_sees_methodics_on_booking_page(self, client, student, lab_work):
        _methodic(lab_work, title="Методичка по ЛР 1")
        client.force_login(student)
        page = client.get(reverse("book-lab-work", args=[lab_work.pk])).content.decode()
        assert "Методичка по ЛР 1" in page
        assert "Просмотреть" in page
        assert "Скачать" in page


@pytest.mark.django_db
class TestMethodicUpload:
    def test_anonymous_cannot_upload(self, client, lab_work):
        response = client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf()},
            follow=True,
        )
        # RoleRequiredMixin отправляет анонима на главную, оттуда — на логин.
        assert response.status_code == 200
        assert "Запись на лабораторные" in response.content.decode()
        assert not LabWorkMethodics.objects.exists()

    def test_lab_head_uploads_pdf(self, client, lab_head, lab_work):
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf(), "title": "Лабораторная практика"},
            follow=True,
        )
        assert response.status_code == 200
        methodic = LabWorkMethodics.objects.get()
        assert methodic.lab_work == lab_work
        assert methodic.uploaded_by == lab_head
        assert methodic.title == "Лабораторная практика"
        assert methodic.file.name.startswith("methodics/")
        assert "загружена" in response.content.decode()
        assert "Лабораторная практика" in response.content.decode()

    def test_lab_head_cannot_upload_to_foreign_lab_work(
        self, client, lab_head, foreign_lab_work
    ):
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-upload", args=[foreign_lab_work.pk]),
            {"file": _pdf()},
            follow=True,
        )
        assert not LabWorkMethodics.objects.exists()
        assert "не найдена в вашей лаборатории" in response.content.decode()

    def test_teacher_cannot_upload(self, client, teacher, lab_work):
        client.force_login(teacher)
        response = client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf()},
            follow=True,
        )
        assert not LabWorkMethodics.objects.exists()
        assert "Доступ только для заведующего лабораторией" in response.content.decode()

    def test_student_cannot_upload(self, client, student, lab_work):
        client.force_login(student)
        client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf()},
        )
        assert not LabWorkMethodics.objects.exists()

    def test_non_pdf_content_rejected(self, client, lab_head, lab_work):
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf(content=b"<html><script>alert(1)</script></html>")},
            follow=True,
        )
        assert not LabWorkMethodics.objects.exists()
        assert "не является PDF" in response.content.decode()

    def test_oversize_rejected(self, client, lab_head, lab_work, monkeypatch):
        monkeypatch.setattr("apps.academics.validators.MAX_METHODIC_BYTES", 10)
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-upload", args=[lab_work.pk]),
            {"file": _pdf(content=PDF_BYTES + b"x" * 100)},
            follow=True,
        )
        assert not LabWorkMethodics.objects.exists()
        assert "больше" in response.content.decode()

    def test_lab_head_deletes_methodic_and_file(self, client, lab_head, lab_work):
        methodic = _methodic(lab_work)
        path = methodic.file.path
        assert os.path.exists(path)
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-delete", args=[lab_work.pk, methodic.pk]),
            follow=True,
        )
        assert response.status_code == 200
        assert not LabWorkMethodics.objects.exists()
        assert not os.path.exists(path)

    def test_deleting_lab_work_removes_methodic_file(self, lab_work):
        methodic = _methodic(lab_work)
        path = methodic.file.path
        assert os.path.exists(path)
        lab_work.delete()
        assert not LabWorkMethodics.objects.exists()
        assert not os.path.exists(path)

    def test_lab_head_cannot_delete_foreign_methodic(
        self, client, lab_head, foreign_lab_work
    ):
        methodic = _methodic(foreign_lab_work)
        client.force_login(lab_head)
        response = client.post(
            reverse("lab-head-methodic-delete", args=[foreign_lab_work.pk, methodic.pk]),
            follow=True,
        )
        assert LabWorkMethodics.objects.filter(pk=methodic.pk).exists()
        assert "не найдена в вашей лаборатории" in response.content.decode()

    def test_portal_shows_upload_form(self, client, lab_head, lab_work):
        _methodic(lab_work, title="Старая методичка")
        client.force_login(lab_head)
        page = client.get(
            reverse("lab-head-lab-works") + f"?methodics={lab_work.pk}"
        ).content.decode()
        assert "Загрузить" in page
        assert "multipart/form-data" in page
        assert "Старая методичка" in page
