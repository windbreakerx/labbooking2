from django.urls import path

from apps.scheduling.views import (
    LabDisciplineBindView,
    LabDisciplineUnbindView,
    LabHeadBindingsView,
    LabHeadHomeView,
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
]
