"""Ролевые предикаты и RoleRequiredMixin'ы: матрица доступа, включая анонима."""

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.users.mixins import (
    LabHeadRequiredMixin,
    StaffRequiredMixin,
    StudentRequiredMixin,
    SysAdminRequiredMixin,
)
from apps.users.models import User, UserRole
from apps.users.roles import (
    can_access_lab_head_portal,
    is_lab_head_user,
    is_staff_user,
    staff_can_manage_catalog,
    staff_can_modify_bookings,
)


def _user(role, email="u@spmi.ru"):
    return User.objects.create_user(
        email=email, password="pass", first_name="R", last_name="O", role=role
    )


ALL_ROLES = [UserRole.STUDENT, UserRole.TEACHER, UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.SYS_ADMIN]


@pytest.mark.django_db
class TestRolePredicates:
    def test_is_staff_user(self):
        assert is_staff_user(AnonymousUser()) is False
        for role in ALL_ROLES:
            assert is_staff_user(_user(role, f"staff-{role}@spmi.ru")) is (
                role != UserRole.STUDENT
            )

    def test_staff_can_modify_bookings(self):
        assert staff_can_modify_bookings(AnonymousUser()) is False
        assert staff_can_modify_bookings(_user(UserRole.TEACHER, "t@spmi.ru")) is False
        for role in (UserRole.LAB_ADMIN, UserRole.LAB_HEAD, UserRole.SYS_ADMIN):
            assert staff_can_modify_bookings(_user(role, f"mod-{role}@spmi.ru")) is True

    def test_staff_can_manage_catalog(self):
        assert staff_can_manage_catalog(_user(UserRole.LAB_ADMIN, "la@spmi.ru")) is False
        for role in (UserRole.LAB_HEAD, UserRole.SYS_ADMIN):
            assert staff_can_manage_catalog(_user(role, f"cat-{role}@spmi.ru")) is True

    def test_lab_head_predicates(self):
        lab_head = _user(UserRole.LAB_HEAD, "lh@spmi.ru")
        assert is_lab_head_user(lab_head) is True
        assert is_lab_head_user(_user(UserRole.SYS_ADMIN, "sa@spmi.ru")) is False
        assert can_access_lab_head_portal(lab_head) is True
        assert can_access_lab_head_portal(_user(UserRole.SYS_ADMIN, "sa2@spmi.ru")) is True
        assert can_access_lab_head_portal(_user(UserRole.LAB_ADMIN, "la2@spmi.ru")) is False
        assert can_access_lab_head_portal(AnonymousUser()) is False


@pytest.mark.django_db
class TestMixins:
    def _access(self, mixin_cls, user):
        return mixin_cls().has_access(user)

    def test_student_mixin(self):
        assert self._access(StudentRequiredMixin, _user(UserRole.STUDENT, "s@stud.spmi.ru"))
        assert not self._access(StudentRequiredMixin, _user(UserRole.LAB_ADMIN, "la3@spmi.ru"))
        assert not self._access(StudentRequiredMixin, AnonymousUser())

    def test_staff_mixin(self):
        assert self._access(StaffRequiredMixin, _user(UserRole.TEACHER, "t2@spmi.ru"))
        assert self._access(StaffRequiredMixin, _user(UserRole.LAB_HEAD, "lh2@spmi.ru"))
        assert not self._access(StaffRequiredMixin, _user(UserRole.STUDENT, "s2@stud.spmi.ru"))
        assert not self._access(StaffRequiredMixin, AnonymousUser())

    def test_lab_head_mixin(self):
        assert self._access(LabHeadRequiredMixin, _user(UserRole.LAB_HEAD, "lh3@spmi.ru"))
        assert self._access(LabHeadRequiredMixin, _user(UserRole.SYS_ADMIN, "sa3@spmi.ru"))
        assert not self._access(LabHeadRequiredMixin, _user(UserRole.LAB_ADMIN, "la4@spmi.ru"))

    def test_sys_admin_mixin(self):
        assert self._access(SysAdminRequiredMixin, _user(UserRole.SYS_ADMIN, "sa4@spmi.ru"))
        assert not self._access(SysAdminRequiredMixin, _user(UserRole.LAB_HEAD, "lh4@spmi.ru"))
        assert not self._access(SysAdminRequiredMixin, AnonymousUser())
