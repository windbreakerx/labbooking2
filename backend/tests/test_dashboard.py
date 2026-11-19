"""Дашборд завлаба: данные графиков не должны ломать разметку.

Название аудитории вводится завлабом, поэтому `</script>` в нём не должен
закрывать script-блок. Шаблон использует json_script, а не json.dumps|safe.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.academics.models import Discipline, LabWork, Semester
from apps.scheduling.models import (
    Laboratory,
    LabSession,
    LabSessionStatus,
    Room,
    TrainingCenter,
)
from apps.users.models import User, UserProfile, UserRole

from .conftest import next_open_weekday_pair


@pytest.fixture
def dashboard_data(db):
    tc = TrainingCenter.objects.create(number=7100, name="Дашборд")
    lab = Laboratory.objects.create(training_center=tc, name="Лаборатория дашборда")
    head = User.objects.create_user(
        email="dash@spmi.ru",
        password="pass",  # nosec B106
        first_name="Д",
        last_name="З",
        role=UserRole.LAB_HEAD,
    )
    UserProfile.objects.create(user=head, laboratory=lab, training_center=tc)

    today = timezone.now().date()
    semester = Semester.objects.create(
        name="Дашборд-семестр",
        start_date=today,
        end_date=today.replace(year=today.year + 1),
        is_active=True,
    )
    room = Room.objects.create(
        training_center=tc,
        number="</script><b>x</b>",
        laboratory=lab,
        capacity=10,
    )
    discipline = Discipline.objects.create(
        title="Дашборд-дисциплина", semester=semester, is_published=True
    )
    lab_work = LabWork.objects.create(
        number=1, title="ЛР дашборда", duration_minutes=90, capacity=3
    )
    lab_work.disciplines.add(discipline)
    lab_work.laboratories.add(lab)

    starts = next_open_weekday_pair(days_ahead=2)
    LabSession.objects.create(
        lab_work=lab_work,
        room=room,
        semester=semester,
        starts_at=starts,
        ends_at=starts + timezone.timedelta(minutes=90),
        status=LabSessionStatus.OPEN,
    )
    return head


@pytest.mark.django_db
def test_dashboard_escapes_room_label(client, dashboard_data):
    client.force_login(dashboard_data)
    response = client.get(reverse("lab-head-dashboard"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "</script><b>x</b>" not in content
    assert "\\u003C/script\\u003E" in content


@pytest.mark.django_db
def test_dashboard_uses_local_chart_js(client, dashboard_data):
    client.force_login(dashboard_data)
    content = client.get(reverse("lab-head-dashboard")).content.decode()

    assert "cdn.jsdelivr.net" not in content
    assert "/static/js/chart.umd.min.js" in content
