"""Dashboard завлаба/сисадмина: KPI, графики Chart.js, быстрые ссылки."""

import json

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
            "room_labels": json.dumps([row["room"] for row in room_data]),
            "room_values": json.dumps([row["utilization"] for row in room_data]),
            "curriculum_labels": json.dumps([row["discipline"] for row in curriculum_data]),
            "curriculum_values": json.dumps([row["progress"] for row in curriculum_data]),
            "no_show_top": no_show_top,
        }
        return render(request, self.template_name, context)
