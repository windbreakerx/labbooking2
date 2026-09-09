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
from apps.scheduling.views.schedule import (
    LabHeadScheduleCopyDayView,
    LabHeadScheduleCreateView,
    LabHeadScheduleDeleteView,
    LabHeadScheduleUpdateView,
    LabHeadScheduleView,
)

__all__ = [
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
    "LabHeadScheduleCopyDayView",
    "LabHeadScheduleCreateView",
    "LabHeadScheduleDeleteView",
    "LabHeadScheduleUpdateView",
    "LabHeadScheduleView",
    "LabSelectView",
    "PortalLaboratoryMixin",
    "SESSION_ADMIN_LAB",
    "resolve_portal_laboratory",
]
