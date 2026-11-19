"""Доступ в Django-админку: флаг is_staff, лимит на логин, очистка флагов.

Админка — отдельный интерфейс: роль SYS_ADMIN сама по себе её не открывает,
а лабораторные роли не должны получать is_staff автоматически.
"""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory

from apps.users.models import User, UserRole
from config.ratelimit import client_ip


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(email, role, *, is_staff=False, is_superuser=False):
    if is_superuser:
        return User.objects.create_superuser(
            email=email,
            password="pass",  # nosec B106
            first_name="А",
            last_name="Б",
            role=role,
        )
    return User.objects.create_user(
        email=email,
        password="pass",  # nosec B106
        first_name="А",
        last_name="Б",
        role=role,
        is_staff=is_staff,
    )


@pytest.mark.django_db
class TestAdminLogin:
    def test_login_page_available(self, client):
        assert client.get("/admin/login/").status_code == 200

    def test_sysadmin_logs_in(self, client):
        _user("root@spmi.ru", UserRole.SYS_ADMIN, is_superuser=True)
        response = client.post(
            "/admin/login/",
            {"username": "root@spmi.ru", "password": "pass", "next": "/admin/"},
        )
        assert response.status_code == 302
        assert response.url == "/admin/"

    def test_login_ratelimited(self, client):
        _user("root@spmi.ru", UserRole.SYS_ADMIN, is_superuser=True)
        with patch("time.time", return_value=1_000_000.0):
            for _ in range(10):
                response = client.post(
                    "/admin/login/",
                    {"username": "root@spmi.ru", "password": "wrong"},
                )
                assert response.status_code == 200
            response = client.post(
                "/admin/login/", {"username": "root@spmi.ru", "password": "pass"}
            )
            assert response.status_code == 403

    def test_login_ratelimit_is_per_client_ip(self, client):
        _user("root@spmi.ru", UserRole.SYS_ADMIN, is_superuser=True)
        with patch("time.time", return_value=1_000_000.0):
            for _ in range(10):
                client.post(
                    "/admin/login/",
                    {"username": "root@spmi.ru", "password": "wrong"},
                    headers={"X-Real-IP": "10.0.0.1"},
                )
            blocked = client.post(
                "/admin/login/",
                {"username": "root@spmi.ru", "password": "pass"},
                headers={"X-Real-IP": "10.0.0.1"},
            )
            assert blocked.status_code == 403

            allowed = client.post(
                "/admin/login/",
                {"username": "root@spmi.ru", "password": "pass", "next": "/admin/"},
                headers={"X-Real-IP": "10.0.0.2"},
            )
            assert allowed.status_code == 302

    def test_lab_head_without_is_staff_cannot_open_admin(self, client):
        _user("head@spmi.ru", UserRole.LAB_HEAD)
        client.force_login(User.objects.get(email="head@spmi.ru"))
        response = client.get("/admin/")
        assert response.status_code == 302
        assert "/admin/login/" in response.url


@pytest.mark.django_db
class TestClientIpKey:
    def _request(self, **meta):
        return RequestFactory().get("/", **meta)

    def test_prefers_real_ip(self):
        request = self._request(
            HTTP_X_REAL_IP="1.1.1.1",
            HTTP_X_FORWARDED_FOR="2.2.2.2",
            REMOTE_ADDR="3.3.3.3",
        )
        assert client_ip("test", request) == "1.1.1.1"

    def test_falls_back_to_forwarded_for(self):
        request = self._request(
            HTTP_X_FORWARDED_FOR="2.2.2.2, 3.3.3.3", REMOTE_ADDR="4.4.4.4"
        )
        assert client_ip("test", request) == "2.2.2.2"

    def test_falls_back_to_remote_addr(self):
        request = self._request(REMOTE_ADDR="4.4.4.4")
        assert client_ip("test", request) == "4.4.4.4"


@pytest.mark.django_db
class TestSyncAdminAccess:
    def test_dry_run_reports_and_keeps_flags(self):
        head = _user("head@spmi.ru", UserRole.LAB_HEAD, is_staff=True)
        out = StringIO()
        call_command("sync_admin_access", stdout=out)
        head.refresh_from_db()
        assert head.is_staff is True
        assert "head@spmi.ru" in out.getvalue()
        assert "Найдено 1" in out.getvalue()

    def test_apply_strips_roles_but_keeps_sysadmin(self):
        head = _user("head@spmi.ru", UserRole.LAB_HEAD, is_staff=True)
        teacher = _user("teacher@spmi.ru", UserRole.TEACHER, is_staff=True)
        root = _user("root@spmi.ru", UserRole.SYS_ADMIN, is_superuser=True)

        call_command("sync_admin_access", "--apply")

        head.refresh_from_db()
        teacher.refresh_from_db()
        root.refresh_from_db()
        assert head.is_staff is False
        assert teacher.is_staff is False
        assert root.is_staff is True and root.is_superuser is True

    def test_nothing_to_do(self):
        _user("root@spmi.ru", UserRole.SYS_ADMIN, is_superuser=True)
        out = StringIO()
        call_command("sync_admin_access", stdout=out)
        assert "Лишних доступов" in out.getvalue()
