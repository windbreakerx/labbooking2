from django.urls import path

from apps.academics.views import MethodicFileView

urlpatterns = [
    path("methodics/<int:pk>/", MethodicFileView.as_view(), name="methodic-file"),
]
