from django.urls import path

from apps.scheduling.views import (
    LabDisciplineBindView,
    LabDisciplineUnbindView,
    LabHeadBindingsView,
    LabHeadHomeView,
    LabHeadLabWorkCreateView,
    LabHeadLabWorkDeleteView,
    LabHeadLabWorksView,
    LabHeadLabWorkUpdateView,
    LabHeadRoomCreateView,
    LabHeadRoomsView,
    LabHeadRoomUpdateView,
    LabHeadScheduleCopyDayView,
    LabHeadScheduleCreateView,
    LabHeadScheduleDeleteView,
    LabHeadScheduleUpdateView,
    LabHeadScheduleView,
    LabSelectView,
)

urlpatterns = [
    path("", LabHeadHomeView.as_view(), name="lab-head-home"),
    path("bindings/", LabHeadBindingsView.as_view(), name="lab-head-bindings"),
    path(
        "bindings/disciplines/<int:pk>/bind/",
        LabDisciplineBindView.as_view(),
        name="lab-head-discipline-bind",
    ),
    path(
        "bindings/disciplines/<int:pk>/unbind/",
        LabDisciplineUnbindView.as_view(),
        name="lab-head-discipline-unbind",
    ),
    path("select-lab/", LabSelectView.as_view(), name="lab-head-select-lab"),
    path("schedule/", LabHeadScheduleView.as_view(), name="lab-head-schedule"),
    path("schedule/create/", LabHeadScheduleCreateView.as_view(), name="lab-head-schedule-create"),
    path(
        "schedule/<int:pk>/update/",
        LabHeadScheduleUpdateView.as_view(),
        name="lab-head-schedule-update",
    ),
    path(
        "schedule/<int:pk>/delete/",
        LabHeadScheduleDeleteView.as_view(),
        name="lab-head-schedule-delete",
    ),
    path("schedule/copy-day/", LabHeadScheduleCopyDayView.as_view(), name="lab-head-schedule-copy-day"),
    path("rooms/", LabHeadRoomsView.as_view(), name="lab-head-rooms"),
    path("rooms/create/", LabHeadRoomCreateView.as_view(), name="lab-head-room-create"),
    path("rooms/<int:pk>/update/", LabHeadRoomUpdateView.as_view(), name="lab-head-room-update"),
    path("lab-works/", LabHeadLabWorksView.as_view(), name="lab-head-lab-works"),
    path("lab-works/create/", LabHeadLabWorkCreateView.as_view(), name="lab-head-lab-work-create"),
    path(
        "lab-works/<int:pk>/update/",
        LabHeadLabWorkUpdateView.as_view(),
        name="lab-head-lab-work-update",
    ),
    path(
        "lab-works/<int:pk>/delete/",
        LabHeadLabWorkDeleteView.as_view(),
        name="lab-head-lab-work-delete",
    ),
]
