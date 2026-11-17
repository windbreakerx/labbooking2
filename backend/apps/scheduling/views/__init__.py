"""Пакет views портала завлаба/сисадмина: портал, редактор расписания, каталог."""

from apps.scheduling.views.catalog import (
    LabHeadLabWorkCreateView,
    LabHeadLabWorkDeleteView,
    LabHeadLabWorksView,
    LabHeadLabWorkUpdateView,
    LabHeadRoomCreateView,
    LabHeadRoomsView,
    LabHeadRoomUpdateView,
)
from apps.scheduling.views.dashboard import LabHeadDashboardView
from apps.scheduling.views.portal import (
    SESSION_ADMIN_LAB,
    LabDisciplineBindView,
    LabDisciplineUnbindView,
    LabHeadBindingsView,
    LabHeadHomeView,
    LabSelectView,
    PortalLaboratoryMixin,
    resolve_portal_laboratory,
)
from apps.scheduling.views.schedule import LabHeadScheduleView

__all__ = [
    "SESSION_ADMIN_LAB",
    "LabDisciplineBindView",
    "LabDisciplineUnbindView",
    "LabHeadBindingsView",
    "LabHeadHomeView",
    "LabHeadLabWorkCreateView",
    "LabHeadLabWorkDeleteView",
    "LabHeadLabWorkUpdateView",
    "LabHeadLabWorksView",
    "LabHeadRoomCreateView",
    "LabHeadRoomUpdateView",
    "LabHeadRoomsView",
    "LabHeadScheduleView",
    "LabHeadDashboardView",
    "LabSelectView",
    "PortalLaboratoryMixin",
    "resolve_portal_laboratory",
]
