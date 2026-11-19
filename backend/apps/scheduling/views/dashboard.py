"""Dashboard завлаба/сисадмина: KPI, графики Chart.js, быстрые ссылки."""

from django.shortcuts import render
from django.views import View

from apps.bookings.reports import (
    dashboard_curriculum_summary,
    dashboard_kpi,
    dashboard_no_show_top,
    dashboard_room_utilization,
)
from apps.scheduling.views.portal import PortalLaboratoryMixin


class LabHeadDashboardView(PortalLaboratoryMixin, View):
    """Главная панель завлаба с KPI и графиками."""

    template_name = "scheduling/portal/dashboard.html"

    def get(self, request):
        laboratory = self.laboratory
        kpi = dashboard_kpi(laboratory)
        room_data = dashboard_room_utilization(laboratory)
        curriculum_data = dashboard_curriculum_summary(laboratory)
        no_show_top = dashboard_no_show_top(laboratory)
        context = {
            "laboratory": laboratory,
            "kpi": kpi,
            # Списки, не json.dumps: шаблон сериализует через json_script,
            # который экранирует </script> в названиях комнат и дисциплин.
            "room_labels": [row["room"] for row in room_data],
            "room_values": [row["utilization"] for row in room_data],
            "curriculum_labels": [row["discipline"] for row in curriculum_data],
            "curriculum_values": [row["progress"] for row in curriculum_data],
            "no_show_top": no_show_top,
        }
        return render(request, self.template_name, context)
